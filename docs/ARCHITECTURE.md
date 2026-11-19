# Architecture

Clipbait is a single Python package with one process model for both long videos
and live streams. The design constraints, in priority order:

1. **No external services.** One box, one SQLite file, one directory of media.
2. **Zero-key usefulness.** Local Whisper when present, a deterministic
   transcriber otherwise; heuristic ranking when no LLM is configured.
3. **Explainability.** Every cut and every score can say why it happened. The
   chat-spike evidence and the forecast components are persisted alongside the
   clip.
4. **Small blast radius.** Media is only served by clip id, every remote URL is
   SSRF-checked, and all writes go under `CLIPBAIT_DATA_DIR`.

---

## Process model

```
                 ┌───────────────────────────────────────────────┐
   HTTP / CLI ──▶│ FastAPI app  (uvicorn, threads)               │
                 │  ├─ REST + SSE                                │
                 │  └─ job submit ──▶ ThreadPoolExecutor         │
                 └───────────────┬───────────────────────────────┘
                                 │
                  ┌──────────────┴───────────────┐
                  │                              │
        process_project                 LiveRunner (thread/monitor)
        (long video / VOD)               ├─ chat source thread
                  │                      ├─ rolling recorder
        ingest → transcribe →            └─ spike → capture thread
        analyze → rank → render                 │
                  │                              ▼
                  └──────────────▶ SQLite + files under data/
```

Choices and why:

* **Threads, not Celery.** The expensive work is `ffmpeg` and C-extension
  Whisper, both of which release the GIL. Avoiding a broker means
  `pip install . && clipbait serve` is the entire deployment. `jobs.py` exposes
  `submit` / `progress` / `cancel`, so a Redis backend can replace it without
  touching the pipeline.
* **SQLite via stdlib `sqlite3`.** WAL mode, `check_same_thread=False`, one
  connection per call. No ORM. Schema lives in `db.py` and is applied
  idempotently on startup.
* **SSE, not WebSockets.** Progress is one-directional and every event is also
  persisted, so a dropped connection is recoverable by polling. `events.py`
  keeps a short history ring plus live queues and hops back to the server loop
  with `call_soon_threadsafe`.

---

## Module map

| Module | Responsibility | Key entry points |
|---|---|---|
| `config.py` | Env → frozen `Settings`, capability detection | `get_settings`, `detect_capabilities` |
| `schema.py` | Pydantic request/response models, options validation | `ProjectOptions`, `MonitorOptions`, `Clip` |
| `db.py` | Schema, CRUD, list queries | `create_project`, `list_clips`, `create_moment` |
| `storage.py` | Path layout, atomic JSON, uploads | `project_dir`, `clip_dir`, `write_json` |
| `jobs.py` | Thread pool, progress, cancellation | `submit`, `progress`, `cancel` |
| `events.py` | In-process pub/sub + history | `bus.publish`, `bus.subscribe` |
| `media.py` | ffmpeg/ffprobe wrappers, encoder validation | `probe`, `extract_audio`, `hw_encoder` |
| `net_guard.py` | SSRF guard for user URLs | `ensure_safe_url` |
| `pipeline.py` | **The only orchestration layer** | `process_project`, `process_live_moment`, `render_clip_record` |
| `ingest/` | yt-dlp and local ingestion | `fetch_remote`, `ingest_local` |
| `transcribe/` | Whisper / OpenAI / Gemini / mock chain | `get_transcriber` |
| `analyze/` | LLM prompts, heuristic ranker, merge, packaging | `analyze`, `packaging_for` |
| `reframe/` | Face sampling, crop path, layout plan | `plan_reframe` |
| `captions/` | Presets + ASS document builder | `build_captions`, `write_ass` |
| `render/` | ffmpeg filter graph + progress parsing | `render_clip` |
| `live/` | Chat sources, spike detector, recorder, runner | `start`, `stop`, `spike`, `recorder` |
| `virality/` | Region profiles, meme formats, variants, forecast | `build_variants`, `forecast` |
| `memes/` | Meme **video** inserts: planner, providers, asset prep | `plan_inserts`, `prepare_insert` |
| `api/app.py` | HTTP surface, SSE, SPA mount | `app`, `create_app` |
| `cli.py` | `serve` / `process` / `live` / `doctor` | `main` |

Rule of thumb: **only `pipeline.py` composes modules.** API handlers validate
input, write a row and submit a job; CLI commands build options and call the
same pipeline functions. That keeps the two front doors behaviourally identical.

---

## Long-video pipeline

`process_project(project_id)`:

1. **Ingest** — local files are hard-linked (falling back to copy); remote URLs
   go through yt-dlp after `ensure_safe_url`. A yt-dlp `.part` file and sidecars
   are cleaned up.
2. **Probe** — duration, dimensions, audio presence.
3. **Audio** — 16 kHz mono WAV at `source/audio.wav`.
4. **Transcribe** — cached at `transcript.json`; the provider chain is
   `faster-whisper → OpenAI → Gemini → mock`. The provider name is stored, and
   `mock` sets a visible warning in project metadata.
5. **Analyze** — audio RMS energy plus either an LLM pass over chunked
   transcripts or the deterministic ranker. Candidates are merged and deduped.
6. **Rank** — overlap suppression (against the shorter clip), min/max duration
   bounds, weak-tail removal, then calibration so the best clip tops out at 92
   rather than 100.
7. **Persist** — clips and packaging variants are written; the project moves to
   `rendering`.
8. **Render** — one ffmpeg process per clip, sequentially by default
   (`CLIPBAIT_RENDER_CONCURRENCY` to parallelise).
9. **Finish** — project `ready` (or `failed` with a bounded error string).

Progress is a sequence of `jobs.progress(...)` calls carrying `(fraction, step,
message)`; the SPA shows steps as a pipeline bar.

### Render graph

`reframe.plan_reframe` decides a layout once per clip:

| Mode | When | Graph |
|---|---|---|
| `track` | wide source, face samples available | scale → `sendcmd` → animated `crop@cam` |
| `center` | wide source, no faces / no OpenCV | static centred crop |
| `blur` | source already vertical or too narrow | blurred `increase` bed + contained foreground |
| `split` | two stable speaker positions | crop each half, stack vertically |

The camera path is one `crop x` value per frame, smoothed, deadzoned to ignore
sub-1% jitter, then **de-duplicated into a `sendcmd` file** so ffmpeg receives
hundreds of commands instead of tens of thousands. Captions are burned with the
`ass` filter; if captions are disabled the layout node is followed by `null`.

Encoding is `libx264` by default. `media.hw_encoder` doesn't trust ffmpeg's
encoder list: it test-encodes three frames with the candidate and caches the
result, so "NVENC listed but no GPU" degrades to CPU instead of failing a render.

---

## Live pipeline

`LiveRunner` (one thread per monitor) owns a single clock:

1. Reset `started_at`, **then** start the recorder, **then** resolve chat.
2. Chat timestamps are rebased by `_chat_offset` (chat start − monitor start) so
   a slow stream resolve cannot shift the spike 30 seconds off the video.
3. Each message goes to `SpikeDetector.add`; every 1.5 s the pending buffer is
   flushed to SQLite and a `stats` event is published.
4. A candidate creates a `moment` row, publishes a `spike` event, and spawns a
   capture thread.
5. The capture thread waits for post-roll, slices the ring buffer into
   `<moment_id>.mp4`, creates a project/clip and calls `process_live_moment`.
6. That path transcribes the already-trimmed moment (timings start at zero),
   builds variants + forecast, and reuses `render_clip_record`.

The chat loop never blocks on rendering. Details and tuning: [`LIVE.md`](LIVE.md).

---

## Data model

```
projects ──1:N── clips
    │
    └──1:N── jobs

monitors ──1:N── moments
    │
    └──1:N── chat_events
```

* `projects.options` / `monitors.options` are JSON blobs validated by Pydantic
  on write, so old rows stay readable as options evolve.
* `clips.meta` holds the packaging variants, forecast, layout, chat overlay and
  render stats. It is the only "loose" field, and it is always additive.
* `moments.signal` stores the full spike evidence: rate z-score, author z-score,
  hype, repeats, sentiment, top messages.
* IDs are prefixed (`prj_`, `clip_`, `job_`, `mon_`, `mom_`) so they are
  recognisable in logs and safe to expose.

### Filesystem

```
<data>/
  clipbait.db
  projects/<prj_id>/
    source/video.mp4
    source/audio.wav
    transcript.json
    clips/<clip_id>/clip.mp4
    clips/<clip_id>/clip.jpg
    clips/<clip_id>/clip.ass
    clips/<clip_id>/crop.cmd
  live/<mon_id>/
    seg_000001.ts ...        # rolling ring
    <mom_id>.mp4             # captured moment
    demo_source.mp4          # demo mode cache
  models/                    # Whisper cache
```

Paths in the database are relative to `<data>` (`storage.relative`), so the
directory can be moved or backed up as one unit. `storage.resolve` refuses to
escape it.

---

## HTTP surface

Everything is under `/api/v1`. Lists return plain arrays; errors are
`{"detail": "..."}` with a real status code.

```
GET    /healthz                 liveness
GET    /meta                    version, capabilities, presets, regions, memes
GET    /projects
POST   /projects                {url, title?, options}
POST   /projects/upload         multipart: file, options(JSON), title?
GET    /projects/{id}           project + clips + jobs + transcript flag
POST   /projects/{id}/retry
DELETE /projects/{id}
GET    /projects/{id}/events    SSE: created/progress/log/done/error
GET    /projects/{id}/clips
GET    /clips/{id}
PATCH  /clips/{id}              title/hook/range/preset/reframe
POST   /clips/{id}/render
POST   /clips/{id}/variants/{variant_id}
GET    /clips/{id}/video
GET    /clips/{id}/thumb
GET    /live/monitors
POST   /live/monitors           {url, title?, options}
POST   /live/monitors/{id}/start|stop
GET    /live/monitors/{id}/chat
GET    /live/monitors/{id}/moments
GET    /live/monitors/{id}/events
POST   /live/demo
```

`PATCH /clips` re-validates the range against project duration; a failed
validation is a 400, not a silent clamp. Media endpoints return 404 until the
file exists; nothing accepts a path.

The SPA is mounted at `/` from `apps/web/dist` with a catch-all that refuses
`api/`, `docs` and `openapi.json`. `CLIPBAIT_WEB_DIST` overrides the location
(the Docker image uses it).

---

## Security posture

* **SSRF** — `ensure_safe_url` allows only `http(s)`, rejects embedded
  credentials, resolves the host and blocks loopback, private, link-local and
  reserved ranges. DNS failures are errors, not passes. `CLIPBAIT_ALLOW_PRIVATE_URLS`
  is the explicit, documented escape hatch for LAN deployments.
* **Path traversal** — uploads are renamed through `safe_filename`; media is
  served by id only; stored paths are relative and resolved under the data dir.
* **XSS** — the SPA renders API strings as text. No `dangerouslySetInnerHTML`
  anywhere in `apps/web/src`.
* **Secrets** — read from the environment (or `.env`). No key has a default, no
  key is logged, and `/meta` reports only *which* provider is active.
* **Resource bounds** — upload size is enforced by the server, clip count is
  capped at 20 per project, target length at 180 s, live ring buffer at 15 min,
  and job error strings are truncated before storage.
* **Telemetry** — none. `CLIPBAIT_TELEMETRY` exists only to make that explicit.

---

## Failure behaviour

| Failure | Behaviour |
|---|---|
| ffmpeg/ffprobe missing | `doctor` reports it; API returns a clear error before queueing |
| No GPU despite NVENC listed | test-encode fails once, cached fallback to `libx264` |
| No Whisper and no keys | `MockTranscriber`, flagged in UI and project metadata |
| LLM timeout / bad JSON | logged, falls back to the heuristic ranker for that run |
| No faces detected | `center` layout instead of failing |
| Live chat source dies | monitor marked `failed` with the error; recording stops cleanly |
| Render fails | clip marked `failed`, project continues with remaining clips |
| SSE client disconnects | events persist; client can poll `/projects/{id}` |

---

## Extension points

* **New market** — append a `RegionProfile` in `virality/regions.py`.
* **New meme format** — append a `MemeFormat` in `virality/memes.py`; selection
  is signal-driven, so no prompt change is needed.
* **New LLM provider** — implement `_provider` in `analyze/llm.py` and add it to
  the capability map.
* **New chat transport** — implement the chat source protocol in `live/chat.py`
  (`name`, `started`, `messages()`), then register it in `resolve_chat_source`.
* **New queue** — keep `jobs.submit/progress/cancel` signatures and swap the
  executor for Redis/RQ.
* **New caption look** — add a `CaptionStyle` in `captions/presets.py`; the ASS
  builder, API catalog and UI picker all read from that dict.

If a change needs a new orchestration step, it belongs in `pipeline.py` — not in
the API layer.