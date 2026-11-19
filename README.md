# Clipbait

**Bait the scroll.** Open-source, local-first engine that turns long videos and live streams into vertical clips built to travel.

Clipbait downloads a source, finds the moments worth cutting, reframes them to 9:16, burns word-level captions, and then packages each clip for a specific **country, platform and meme format** — with an explainable virality forecast instead of a black-box score.

No account. No cloud required. No API key needed to render a clip.

```
long video / live stream
        │
        ├─ chat spikes ──▶ rolling buffer ──▶ clip the reaction (pre-roll included)
        │
        └─ transcript ──▶ highlight ranker ──▶ reframe ──▶ captions ──▶ packaging
                                              (9:16)      (word-level)   (region + meme + forecast)
```

---

## Why another clipper

Most open-source clippers stop at "cut the loud part and add captions". Clipbait focuses on the two things that actually decide whether a clip travels:

1. **Live moments, not just VODs.** A rolling recorder keeps the last three minutes on disk and a robust chat-spike detector (median/MAD rate z-score, unique-author surge, hype lexicon, duplicate ratio, sentiment) triggers the cut. The clip starts *before* the trigger, so the reaction is inside the frame.
2. **Distribution packaging.** Every clip ships with ranked variants for a market: native hook, hashtags, CTA, best posting window and a matched meme format. The same 40-second story becomes a different product in `US` vs `IN` vs `BR`.

And one engineering constraint: **the zero-key path is a first-class product**. Local Whisper when installed, a deterministic transcriber otherwise, heuristic ranking when no LLM is configured, CPU rendering when no GPU is usable.

---

## Feature matrix

| | Clipbait | Typical OSS clipper | Hosted clipper |
|---|---|---|---|
| Long video → vertical clips | ✅ | ✅ | ✅ |
| Word-level animated captions (7 presets) | ✅ | partial | ✅ |
| Face-aware virtual camera (`sendcmd` crop) | ✅ | partial | ✅ |
| Blur bed / split-screen layouts | ✅ | partial | ✅ |
| **Live chat spike detection → auto-clip** | ✅ | ❌ | paid tier |
| **Region playbooks (26 markets)** | ✅ | ❌ | ❌ |
| **Meme-format matching (26 formats)** | ✅ | ❌ | ❌ |
| **Meme video b-roll inserts (gap/signal-triggered)** | ✅ | ❌ | ❌ |
| **Explainable 0–100 forecast** | ✅ | ❌ | opaque score |
| Runs fully offline | ✅ | ✅ | ❌ |
| Zero API keys to render | ✅ | usually | ❌ |
| Local SQLite + files, no external DB | ✅ | varies | ❌ |

> Forecasts are directional editing heuristics, not guarantees. See [Responsible use](#responsible-use).

---

## Quickstart

### Docker (recommended for a clean box)

```bash
git clone https://github.com/<you>/Clipbait.git
cd Clipbait
mkdir -p data
docker compose up --build
# open http://localhost:8000
```

The compose file mounts `./data` so projects, the SQLite database and rendered clips survive restarts. It runs as UID/GID `1000` by default so the bind mount is writable; if your user differs, export `CLIPBAIT_UID=$(id -u) CLIPBAIT_GID=$(id -g)` first. Prefer a different port? `CLIPBAIT_PORT=9000 docker compose up`.

### Local

Requirements: **Python 3.11+**, **ffmpeg/ffprobe**, **yt-dlp** (for URL sources). Node is only needed if you change the UI.

```bash
git clone https://github.com/<you>/Clipbait.git
cd Clipbait

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Optional upgrades — each one is independently useful:
pip install -e ".[whisper]"   # local word-level transcription (faster-whisper)
pip install -e ".[vision]"    # face-aware camera tracking (OpenCV)
pip install -e ".[live]"      # keyless YouTube live chat (pytchat)

clipbait doctor               # what is installed, what is missing
clipbait serve                # dashboard + API on http://127.0.0.1:8000
```

The built SPA is committed under `apps/web/dist`, so `clipbait serve` has a UI immediately. Rebuild it only when you edit the frontend:

```bash
make web        # or: cd apps/web && npm install && npm run build
```

> Installing the wheel outside a checkout (no `apps/web/dist`)? The API still
> works; point `CLIPBAIT_WEB_DIST` at a built bundle if you want the UI.

### macOS / Linux one-liners

```bash
# Debian/Ubuntu
sudo apt install ffmpeg pipx && pipx install yt-dlp

# macOS
brew install ffmpeg yt-dlp
```

---

## Usage

### Dashboard

Open `http://127.0.0.1:8000`:

* **Projects** — every long-video run, live progress over SSE, rendered clip cards.
* **New clip** — URL or upload, region + platform, caption preset, reframe mode, variant count.
* **Live** — start a monitor from a YouTube/Twitch URL, or run the synthetic demo stream.
* **Playbooks** — browse all 26 region profiles, 26 meme formats, caption looks and detected capabilities.

### CLI

```bash
# Long video (local file or URL)
clipbait process ./podcast.mp4 --clips 6 --target-seconds 50 \
  --region IN --platform reels --preset lime

# A YouTube/Twitch VOD
clipbait process "https://www.youtube.com/watch?v=..." --region US --platform tiktok

# Watch live chat and clip the spikes (demo needs no network)
clipbait live demo --threshold 2.5
clipbait live "https://www.twitch.tv/somechannel" --region BR --platform reels

# Inspect the environment
clipbait doctor
```

Rendered files land in `data/projects/<project_id>/clips/<clip_id>/clip.mp4`, alongside `clip.ass`, a thumbnail and the packaging metadata in SQLite.

### API

FastAPI serves an OpenAPI schema at `/docs` and `/openapi.json`. All endpoints live under `/api/v1`:

```
GET    /meta
GET    /projects
POST   /projects                 {url, options, title}
POST   /projects/upload          multipart: file, options(JSON), title
GET    /projects/{id}
POST   /projects/{id}/retry
DELETE /projects/{id}
GET    /projects/{id}/events     SSE progress
GET    /clips/{id}
PATCH  /clips/{id}
POST   /clips/{id}/render
POST   /clips/{id}/variants/{variant_id}
GET    /clips/{id}/video
GET    /clips/{id}/thumb
GET    /live/monitors
POST   /live/monitors
POST   /live/monitors/{id}/start|stop
GET    /live/monitors/{id}/chat
GET    /live/monitors/{id}/moments
GET    /live/monitors/{id}/events   SSE spike/clip events
POST   /live/demo
```

Media is only served by clip id (never by user-supplied path), and every URL source passes an SSRF guard before download.

---

## How it works

```
ingest ──▶ transcribe ──▶ analyze ──▶ ranking ──▶ reframe ──▶ captions ──▶ render
  │            │             │            │           │           │
  │            │             │            │           │           └─ ASS: one Dialogue
  │            │             │            │           │              event per word
  │            │             │            │           └─ per-frame crop x
  │            │             │            │              (deduped sendcmd) or
  │            │             │            │              blur/split graph
  │            │             │            └─ overlap suppression, duration
  │            │             │               bounds, score calibration
  │            │             └─ LLM if configured, heuristic ranker always
  │            └─ faster-whisper → OpenAI → Gemini → deterministic mock
  └─ yt-dlp for YouTube/Twitch/Vimeo/direct URLs, hard-link for local files
```

Then **packaging** runs per clip: `virality/regions.py` + `virality/memes.py` + `virality/packaging.py` produce ranked variants and a forecast. Rendering is one ffmpeg process per clip; progress is parsed from `-progress pipe:1`. Hardware encoders are *test-encoded* before use, because "listed by ffmpeg" is not the same as "the GPU is reachable".

### Project layout

```
src/clipbait/
  api/          FastAPI app, SSE, SPA mount
  ingest/       yt-dlp + local ingestion
  transcribe/   whisper / OpenAI / Gemini / deterministic mock
  analyze/      LLM + zero-key heuristic ranking, merging
  reframe/      face sampling, camera path, sendcmd, blur/split
  captions/     presets + ASS generation (karaoke active word)
  render/       ffmpeg filter graphs and progress
  live/         chat sources, spike detector, rolling recorder, orchestrator
  virality/     26 region profiles, 26 meme formats, variants, forecast
  pipeline.py   the only orchestration entry points
  db.py         stdlib sqlite3, WAL, thread-safe
  jobs.py       thread pool (swap for Redis later)
apps/web/       Vite + React + TypeScript SPA (no UI framework)
tests/          spike logic, captions, ranking, virality, API, reframe
```

---

## Live clipping

Clipbait treats live chat as the highlight signal.

* **Rolling capture**: 4-second MPEG-TS segments in a three-minute ring buffer, pruned as it grows.
* **Spike evidence**: rate z-score (median + MAD baseline), unique-author z-score, hype-lexicon density, duplicate-message ratio and sentiment intensity. The trigger stores the evidence, so a clip can explain *why* it was cut.
* **Pre-roll first**: default 25s before the trigger, 20s after. Tune with `--pre-roll` / `--post-roll` in `MonitorOptions`.
* **Non-blocking**: rendering happens on a separate thread; the chat loop keeps capturing.

Chat sources:

| Source | Key needed | Notes |
|---|---|---|
| YouTube (`pytchat`) | none | `pip install 'clipbait[live]'` |
| YouTube Data API | `YOUTUBE_API_KEY` | polling fallback for public streams |
| Twitch IRC | none | anonymous TLS read |
| `demo://live` | none | synthetic stream + scripted chat spikes |

```bash
clipbait live demo --threshold 2.5
```

The demo generates a 4-minute synthetic stream locally, spikes chat three times, and renders clips end-to-end with chat overlay. It is the fastest way to verify the whole live path without a real broadcast.

> Only clip streams you own or have permission to reuse. Respect platform terms and local law. See [Responsible use](#responsible-use).

Details: [`docs/LIVE.md`](docs/LIVE.md).

---

## Region & meme intelligence

`Clipbait` ships 26 market profiles: language(s), humour register, emoji density, default caption preset, evergreen hashtags, native CTA, posting windows in local time, platform bias, slang and topics to avoid.

Examples:

| Code | Market | Humour | Default preset | Posting windows |
|---|---|---|---|---|
| `US` | United States | punchy, confident, direct | `hormozi` | 07–09, 12–13, 18–22 |
| `IN` | India | high-energy, story-first | `lime` | 08–10, 13–15, 20–23 |
| `BR` | Brazil | loud, funny, expressive | `lime` | 11–13, 17–19, 20–23 |
| `DE` | Germany | precise, dry, proof-first | `clean` | 07–09, 12–14, 18–21 |
| `PH` | Philippines | warm, dramatic, relational | `hormozi` | 11–13, 17–19, 21–24 |
| `ZA` | South Africa | warm, blunt, funny | `default` | 07–09, 12–14, 19–22 |

Plus 26 evergreen meme **structures** (`POV`, `Nobody:`, `Expectation vs Reality`, `Sana all`, `Eish…`, `Jugaad`, `Wahala`, …). Formats are matched to content signals (`failure`, `revelation`, `contrast`, `awe`, …), filtered by region/platform, and tagged `low` or `medium` risk. Medium-risk formats require an explicit opt-in.

Add your own market by appending a `RegionProfile` in `src/clipbait/virality/regions.py`; add a format by appending a `MemeFormat` in `virality/memes.py`. Everything is data — no model retraining, no prompt rewrite.

### Meme video inserts

Opt in per project (**Meme video inserts** in the UI, `meme_inserts` in the API,
`CLIPBAIT_MEME_PROVIDER` for the machine default) and a clip can cut to a short
generated b-roll video for a moment, then cut back. Two rules keep it from
degrading the clip:

- **Only if needed.** A cutaway is planned only where the transcript has a
  dead-air **gap** or a **strong signal** (surprise, revelation, failure,
  absurd, ...). A clip with neither gets no insert at all — the common case.
- **Audio and captions never drift.** The insert replaces the **picture only**.
  The original spoken audio keeps running underneath, and word-level captions
  are burned on top of the insert from the same timings. Nothing is re-timed.

Insert assets come from `CLIPBAIT_MEME_API_URL` (a hosted text-to-video API) when
configured. With no key, Clipbait falls back to a locally ffmpeg-rendered
placeholder card so the feature is testable offline — the same zero-key
philosophy as the mock transcriber. A provider failure is a skipped insert, never
a failed render. See [`docs/MEMES.md`](docs/MEMES.md).

Details: [`docs/VIRALITY.md`](docs/VIRALITY.md).

---

## Configuration

Everything is environment-driven. Defaults are safe for a laptop.

| Variable | Default | Purpose |
|---|---|---|
| `CLIPBAIT_DATA_DIR` | `./data` | Projects, live buffers, models, SQLite |
| `CLIPBAIT_DB_PATH` | `<data>/clipbait.db` | SQLite file |
| `CLIPBAIT_HOST` / `CLIPBAIT_PORT` | `127.0.0.1` / `8000` | `clipbait serve` bind |
| `CLIPBAIT_TRANSCRIBER` | `auto` | `auto`, `whisper`, `openai`, `gemini`, `mock` |
| `CLIPBAIT_WHISPER_MODEL` | `small` | faster-whisper model |
| `CLIPBAIT_WHISPER_DEVICE` | `auto` | `auto`, `cpu`, `cuda` |
| `CLIPBAIT_LLM_PROVIDER` | `auto` | `auto`, `openai`, `anthropic`, `gemini`, `ollama`, `none` |
| `OPENAI_API_KEY` | — | OpenAI transcription/ranking |
| `ANTHROPIC_API_KEY` | — | Anthropic ranking |
| `GEMINI_API_KEY` | — | Gemini transcription/ranking |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local LLM |
| `CLIPBAIT_MAX_CLIPS` | `8` | Default clip budget |
| `CLIPBAIT_TARGET_CLIP_SECONDS` | `55` | Default target length |
| `CLIPBAIT_RENDER_CONCURRENCY` | `1` | Parallel render workers |
| `CLIPBAIT_HW_ENCODER` | `auto` | `cpu`, `nvenc`, `qsv`, `vaapi`, `videotoolbox` |
| `CLIPBAIT_MEME_PROVIDER` | `auto` | Meme video inserts: `auto`, `api`, `fixture`, `none` |
| `CLIPBAIT_MEME_API_URL` | — | Hosted text-to-video endpoint for meme inserts |
| `CLIPBAIT_MEME_API_KEY` | — | Bearer token for the meme API |
| `CLIPBAIT_MEME_MODEL` | — | Optional model id sent to the meme API |
| `CLIPBAIT_MEME_POLL_SECONDS` | `240` | Async generation poll budget |
| `CLIPBAIT_LIVE_BUFFER_SECONDS` | `180` | Ring buffer depth |
| `CLIPBAIT_LIVE_SEGMENT_SECONDS` | `4` | HLS segment size |
| `CLIPBAIT_LIVE_CHAT_PROVIDER` | `auto` | `auto`, `youtube`, `twitch`, `demo` |
| `YOUTUBE_API_KEY` | — | YouTube Data API chat fallback |
| `CLIPBAIT_ALLOW_PRIVATE_URLS` | `0` | Allow LAN/localhost ingestion (disables SSRF guard) |
| `CLIPBAIT_CORS_ORIGINS` | `*` | Comma-separated origins |
| `CLIPBAIT_PUBLIC_BASE_URL` | `http://localhost:8000` | Links printed by the CLI |
| `CLIPBAIT_WEB_DIST` | auto-detected | Location of the built SPA |
| `CLIPBAIT_REGION_OVERRIDES` | — | JSON patch for market profiles (see `docs/VIRALITY.md`) |
| `CLIPBAIT_TELEMETRY` | `0` | Reserved; Clipbait never phones home |

Copy `.env.example` to `.env` for a starting point. Existing environment
variables always win over `.env`, so containers and CI behave predictably.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite covers the highest-risk pure logic: spike detection (including median/MAD and cooldown), segment selection, ASS generation for every preset, ranking/overlap suppression, region + meme packaging, meme-insert planning and render graph, forecast bounds, SSRF blocking, reframe geometry, transcription fallback and the HTTP contract (including SPA fallback).

Frontend:

```bash
cd apps/web
npm install
npm run typecheck
npm run build
```

---

## Limitations

* **Transcription language coverage** depends on the installed Whisper model or cloud provider. `mock` is for demos only and is labelled as such in the UI.
* **Face tracking** needs `opencv-python-headless`; without it Clipbait falls back to a centre crop or blur bed.
* **Live clipping** supports YouTube and Twitch chat transports currently. Kick chat capture is not implemented; Kick VODs work through yt-dlp.
* **Hardware encoders** are validated at runtime. In VMs/containers without a reachable GPU, Clipbait falls back to `libx264`.
* **Posting is not automated.** Clipbait produces files and copy; you decide where they go.

## Responsible use

Clipbait is an editing tool, not a rights bypass.

* Only process video and chat you own, have licensed, or may use under the source platform's terms. Downloading or re-uploading third-party content can infringe copyright.
* Live chat contains personal data. Captured messages stay in your local database and should be handled accordingly (GDPR/CCPA/etc.).
* Region profiles are **averages and starting points**, not cultural truth. Review hooks and slang before publishing; the `avoid` list exists because generic humour is often the fastest way to damage a brand.
* Forecasts are directional. They are not a promise of views, revenue or reach.
* Do not use Clipbait to manufacture harassment, misinformation, or non-consensual content.

---

## Attribution

Clipbait adapts *techniques* — not code wholesale — from MIT-licensed projects, and cites a non-commercial project as design inspiration only:

* [`mutonby/openshorts`](https://github.com/mutonby/openshorts) (MIT) — ASS word-level caption structure, `sendcmd` animated crop approach, split-screen/blur layout ideas.
* [`Anil-matcha/AI-Youtube-Shorts-Generator`](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator) (MIT) — chunked virality prompting patterns.
* [`FujiwaraChoki/supoclip`](https://github.com/FujiwaraChoki/supoclip) — product/UX inspiration only; its license is non-commercial, so **no code was copied**.

See [`NOTICE`](NOTICE) for the full list, including runtime dependencies.

## License

MIT — see [`LICENSE`](LICENSE).