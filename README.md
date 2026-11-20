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
| **On-stream detection (speech, loudness, action)** | ✅ | ❌ | ❌ |
| **Revenue-ranked posting queue + export bundles** | ✅ | ❌ | analytics only |
| **Auto-post to your accounts at the best window** | ✅ | ❌ | paid tier |
| **Unattended autopilot loop (discover→watch→clip→post)** | ✅ | ❌ | ❌ |
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

Running the image directly with `docker run` instead of Compose? Add `--user "$(id -u):$(id -g)"` whenever you bind-mount a host directory, or the app (which runs as UID 10001) cannot write to it. Anonymous volumes need no flags.

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
* **Money queue** — every rendered clip priced by expected revenue, recommended platform, copy-ready caption, one-click export and posted tracking.
* **Playbooks** — browse all 26 region profiles, 26 meme formats, caption looks and detected capabilities.

### CLI

```bash
# Long video (local file or URL)
clipbait process ./podcast.mp4 --clips 6 --target-seconds 50 \
  --region IN --platform reels --preset lime

# A YouTube/Twitch VOD
clipbait process "https://www.youtube.com/watch?v=..." --region US --platform tiktok

# Watch the stream and the chat, clip whatever goes viral (demo needs no network)
clipbait live demo --threshold 2.5
clipbait live "https://www.twitch.tv/somechannel" --region BR --platform reels

# Chat only, or tune on-stream detection independently
clipbait live "https://www.youtube.com/watch?v=..." --no-content
clipbait live "https://www.youtube.com/watch?v=..." --content-threshold 3.2

# You saw the moment yourself: cut exactly that window, nothing else
clipbait grab "https://www.youtube.com/watch?v=..." --at 1:24:30 --seconds 40 \
  --campaign data/campaigns/kai-gta6.md --export
clipbait grab ./local-capture.mp4 --at 0:12 --seconds 30 --language en

# What to post next, ranked by expected revenue
clipbait queue
clipbait queue --json --min-score 60
clipbait queue --campaign data/campaigns/clipmoney.md   # price at the brief's rate
clipbait queue --export
clipbait queue --mark clip_abc123 --url "https://tiktok.com/@me/video/1"

# Re-derive captions the detector wrote for itself (dry run unless --apply)
clipbait recaption
clipbait recaption --apply

# Post them to your accounts, at the best window (dry run unless --live)
clipbait publish                                 # plan every ready clip, send nothing
clipbait publish clip_abc123 --at 2026-11-21T19:30 --timezone America/New_York
clipbait publish clip_abc123 --now --live       # this one, immediately
clipbait publish --live --campaign data/campaigns/clipmoney.md

# Unattended: discover -> watch -> clip -> post. --now posts immediately.
clipbait autopilot --query "gta 6" --platform shorts --now --live

# Find live rooms by audience, then monitor the biggest one
clipbait streams --query "gta 6" --limit 15
clipbait streams --query "gta 6" --watch

# Pre-post check against the campaign rules (exit 1 on a FAIL)
clipbait campaign
clipbait check clip_abc123 --campaign data/campaigns/clipmoney.md

# One unattended pass: find a live room, watch it, package what is ready
./scripts/earn.sh
QUERY="kai cenat" MINUTES=45 CAMPAIGN=data/campaigns/clipmoney.md ./scripts/earn.sh

# The whole loop, unattended: discover -> watch -> clip -> post, round after round
clipbait autopilot --query "gta 6" --rounds 4 --minutes 30 --live \
  --campaign data/campaigns/clipmoney.md
clipbait autopilot --source "https://youtube.com/watch?v=..." --rounds 0   # fixed room, until Ctrl-C

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
POST   /clips/{id}/uploaded      {url?, platform?}
POST   /clips/{id}/export        upload-ready bundle
GET    /queue                    ready clips ranked by expected USD
GET    /live/monitors
POST   /live/monitors
POST   /live/monitors/{id}/start|stop
GET    /live/monitors/{id}/chat
GET    /live/monitors/{id}/moments
GET    /live/monitors/{id}/events   SSE spike/clip events
POST   /live/demo
```

Media is only served by clip id (never by user-supplied path), and every URL source passes an SSRF guard before download.

### Link mode — clip without downloading the whole video

A URL project defaults to link mode (`source_mode: "auto"`). Instead of fetching
the entire file, Clipbait:

1. downloads **audio only** (`bestaudio`, a few MB) for transcription;
2. keeps the moments it wants;
3. fetches only each kept clip's `[start, end]` window with
   `yt-dlp --download-sections`, then renders from that window.

A 40-second clip out of a two-hour stream therefore costs roughly 40 seconds of
transfer. Timings stay exact: the fetched window starts at the clip start, so
every word and chat message is rebased by `-start` and rendered on a timeline
that begins at zero.

Link mode is a fast path, not a hard requirement. If the site cannot serve
ranges, or a section download fails, the pipeline falls back to a normal full
download automatically. Set `source_mode: "download"` (or switch **Link mode**
off in the new-clip form) to force the old behaviour. Local uploads never use
link mode.

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

### Real captions

Captions are not a guess: they are transcribed from the clip's own audio with
word-level timestamps, and each word becomes one ASS `Dialogue` event at the
moment it is spoken. Cuts, inserts and reframing never re-time them, so the
caption text always matches what is heard.

The real path is local **faster-whisper**, which needs no key:

```bash
pip install -e ".[whisper]"
clipbait doctor                 # should say whisper: yes
CLIPBAIT_WHISPER_MODEL=small clipbait serve
```

`CLIPBAIT_WHISPER_MODEL` trades accuracy for speed: `tiny` is fine for a quick
demo (a 2-minute clip transcribes in well under a minute on CPU), `small` is the
better default, and `base`/`medium` sit between them. The model is downloaded
once into `<data>/models`. Without faster-whisper, Clipbait falls back to a
deterministic **mock** transcript so rendering still works — but that text is
synthetic, and both the API (`meta.transcriber`) and the UI say so explicitly.
Cloud transcription (OpenAI/Gemini) is used when its key is present.

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

Clipbait watches the chat **and** the stream itself.

* **Rolling capture**: 4-second MPEG-TS segments in a three-minute ring buffer, pruned as it grows.
* **Chat evidence**: rate z-score (median + MAD baseline), unique-author z-score, hype-lexicon density, duplicate-message ratio and sentiment intensity. The trigger stores the evidence, so a clip can explain *why* it was cut.
* **On-stream evidence**: the newest 32s of buffer is transcribed and scored for hype language, exclamations, pace and repetition; the audio track is checked for a sudden loudness jump against its own baseline; the video track is checked for hard scene-change bursts. A viral line is caught before chat has time to react.
* **Fusion**: a chat spike and an on-stream moment within 12 seconds become one moment, one clip, with both sets of evidence.
* **Pre-roll first**: default 25s before the trigger, 20s after. Tune with `--pre-roll` / `--post-roll` in `MonitorOptions`.
* **Non-blocking**: rendering happens on a separate thread; chat and content detection keep running.
* **Money layer**: every ready clip is priced by expected revenue and recommended platform, then exported as an upload bundle. [`clipbait publish`](#publishing) hands the bundle to your own accounts at the best window. See [`docs/MONEY.md`](docs/MONEY.md).

Chat sources:

| Source | Key needed | Notes |
|---|---|---|
| YouTube (`pytchat`) | none | used when the optional `clipbait[live]` extra is installed |
| YouTube InnerTube | none | keyless fallback; reads the public chat endpoint like the website |
| YouTube Data API | `YOUTUBE_API_KEY` | polling fallback for public streams |
| Twitch IRC | none | anonymous TLS read |
| `demo://live` | none | synthetic stream + scripted chat spikes |

```bash
clipbait live demo --threshold 2.5          # chat + on-stream detection
clipbait live <url> --no-content            # chat only
clipbait live <url> --language en --hashtags "#kaicenat #gta6"
clipbait queue                              # what to post, where, for how much
```

### When you saw it yourself

Detection is a guess about what matters. A person watching the stream is not
guessing, so Clipbait also takes an exact window:

```bash
clipbait grab "https://www.youtube.com/watch?v=..." --at 1:24:30 --seconds 40 \
  --language en --hashtags "#kaicenat #gta6" --campaign data/campaigns/kai-gta6.md --export
```

That fetches only those 40 seconds (yt-dlp range support, so a 40s grab of a
three-hour VOD costs 40s of transfer), transcribes them, cuts the strongest
spoken line into the hook, burns captions, and writes the same upload bundle a
detected moment gets. It is the fastest honest route from "that was funny" to a
postable file, and it works on a finished VOD as well as a live capture.

The demo generates a 4-minute synthetic stream locally, spikes chat three times, and renders clips end-to-end with chat overlay. It is the fastest way to verify the whole live path without a real broadcast.

> Only clip streams you own or have permission to reuse. Respect platform terms and local law. See [Responsible use](#responsible-use).

Details: [`docs/LIVE.md`](docs/LIVE.md) and [`docs/MONEY.md`](docs/MONEY.md).

### Publishing

The queue says *what* to post; `publish` handles *when* and *where*. It is dry by
default: it prints the destination, the caption and the exact moment, and sends
nothing.

```bash
clipbait publish                                  # dry run: plan the whole queue
clipbait publish --live                           # send every ready clip
clipbait publish clip_abc123 --live --now         # this one, immediately
clipbait publish clip_abc123 --at 2026-11-21T19:30 --timezone America/New_York
clipbait publish --live --platform shorts,reels   # force destinations
```

It posts through [Upload-Post](https://upload-post.com), which reaches TikTok,
Instagram, YouTube, LinkedIn, X and more with one request. Set the key and the
profile once:

```bash
export UPLOAD_POST_API_KEY=...        # from the Upload-Post dashboard
export UPLOAD_POST_USER=your_profile  # the profile whose accounts you connected
```

**Or self-host [Postiz](https://github.com/gitroomhq/postiz-app) for unlimited
posts.** One install, no per-upload fee. Connect your channels in Postiz, copy
an API key, and point Clipbait at it:

```bash
export CLIPBAIT_PUBLISH_PROVIDER=postiz
export POSTIZ_API_KEY=...             # Postiz > Settings > API
export CLIPBAIT_POSTIZ_API_URL=http://localhost:4007/api/public/v1
```

Postiz targets a connected channel rather than a profile, so `--user` is not
needed, and it has no measured-best-hours endpoint: the region playbook picks
the window. Everything else — idempotent sends, dry-run default, the clip only
marked posted after acceptance — works the same.

**The window is picked from data, not a guess.** If the connected account has
enough history, Clipbait asks Upload-Post for its *measured* best hours
(`source: account-best-times`). Without them it falls back to the region's
curated posting windows, interpreted in the market's own timezone
(`source: region-window`). Either way the post is scheduled **on Upload-Post's
side** with `scheduled_date` + `timezone`, so a single `--live` run is the whole
automation: nothing has to stay running waiting for the hour to arrive.

Every send is idempotent (`Idempotency-Key: clipbait-<clip_id>`), and a clip is
only marked posted after the provider accepts it. A re-run returns the existing
job instead of posting twice.

### Autopilot

`clipbait autopilot` runs the whole thing unattended, round after round:

```bash
clipbait autopilot --query "gta 6" --rounds 4 --minutes 30 --live
clipbait autopilot --source "https://youtube.com/watch?v=..." --rounds 0  # until Ctrl-C
```

Each round it finds the biggest live room for `--query` (or watches the fixed
`--source`), monitors it for `--minutes` **or** until `--max-clips` clips have
finished rendering (whichever comes first; a clip row appears the moment a moment
is detected but renders on a thread, so only ready clips count and the round
briefly waits out anything still in flight), stops the monitor, packages what is ready, and — only
with `--live` — hands that round's clips to `publish` at their windows. Then it
cools down `--cooldown` seconds and picks the next room it has not already
watched.

Publishing is off unless `--live` is passed; without it the loop still discovers,
watches and clips, so you can watch it work before it ever posts.

The round decisions are pure and tested against a fake clock, and every side
effect (discover, start, stop, export, publish, sleep) sits behind one
replaceable `Runtime`, so the loop is exercised end to end in tests with no
network and no ffmpeg.


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

Insert assets come from `CLIPBAIT_MEME_SEARCH_URL` (search your own meme
library) or `CLIPBAIT_MEME_API_URL` (a hosted text-to-video API) when
configured. The search provider maps any JSON endpoint with two dot paths, so
Giphy, Tenor or a private catalogue can be plugged in without code changes. With
nothing configured, Clipbait falls back to a locally ffmpeg-rendered placeholder
card so the feature is testable offline — the same zero-key philosophy as the
mock transcriber. A provider failure is a skipped insert, never a failed render.
See [`docs/MEMES.md`](docs/MEMES.md) for the exact contract.

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
| `CLIPBAIT_MEME_PROVIDER` | `auto` | Meme video inserts: `auto`, `search`, `api`, `fixture`, `none` |
| `CLIPBAIT_MEME_SEARCH_URL` | — | Searchable meme library endpoint (see `docs/MEMES.md`) |
| `CLIPBAIT_MEME_SEARCH_KEY` | — | Key/token for the meme library |
| `CLIPBAIT_MEME_SEARCH_AUTH` | `bearer` | `bearer`, `header`, `query`, `none` |
| `CLIPBAIT_MEME_SEARCH_RESULTS` | `results` | Dot path to the result list |
| `CLIPBAIT_MEME_SEARCH_VIDEO` | `url` | Dot path to each result's video URL |
| `CLIPBAIT_MEME_API_URL` | — | Hosted text-to-video endpoint for meme inserts |
| `CLIPBAIT_MEME_API_KEY` | — | Bearer token for the meme API |
| `CLIPBAIT_MEME_MODEL` | — | Optional model id sent to the meme API |
| `CLIPBAIT_MEME_POLL_SECONDS` | `240` | Async generation poll budget |
| `CLIPBAIT_LIVE_BUFFER_SECONDS` | `180` | Ring buffer depth |
| `CLIPBAIT_LIVE_SEGMENT_SECONDS` | `4` | HLS segment size |
| `CLIPBAIT_LIVE_CHAT_PROVIDER` | `auto` | `auto`, `pytchat`, `innertube`, `api`, `demo` |
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