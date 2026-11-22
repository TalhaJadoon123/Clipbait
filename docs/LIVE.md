# Live clipping

Most clippers are VOD tools with a live URL bolted on. Clipbait watches two
things at once: the **chat**, which tells you where the audience is losing it,
and the **stream itself** — what the streamer says, screams or does. Chat lags;
the moment is already happening on camera.

```
stream ──▶ rolling recorder ──────────────────────────────┐
              │   (last N minutes of .ts segments)         │
              ├──▶ content watcher ──┐                     │
chat ──▶ spike detector ─────────────┴─▶ moment ──▶ capture ┴─▶ slice.mp4
                                                            │
                                             transcribe ──▶ reframe ──▶
                                             captions   ──▶ packaging ─▶ clip
```

Two detectors can fire for the same beat. They are fused into one moment and one
clip, with the evidence from both.

---

## Why pre-roll matters

The reaction is the clip. If you start recording when chat says "CLIP IT", you
have already lost the moment. Clipbait therefore records continuously into a
ring buffer and cuts **backwards** from the trigger:

```
        pre_roll 25s                  post_roll 20s
  ├───────────────────────┼──────────────────────┤
  reaction starts    chat explodes        aftermath
                        ▲
                    trigger_at
```

Defaults live in `MonitorOptions` (`pre_roll=25`, `post_roll=20`) and are
exposed in the UI and CLI.

---

## The spike detector

`live/spikes.py` is pure logic with no I/O — the most-tested module in the repo.

Chat is bucketed into 4-second windows. Per bucket it computes:

| Signal | Meaning | Weight |
|---|---|---|
| `rate_z` | message-rate z-score against a median/MAD baseline | 0.55 |
| `unique_z` | unique-author z-score | 0.15 |
| `hype` | hype-lexicon hits per message (W, LULW, NO WAY, clip it, 🔥, …) | 0.15 |
| `repeats` | share of the most duplicated message | 0.10 |
| `sentiment` | absolute positive-minus-negative intensity | 0.05 |

```
score = 0.55·max(0, rate_z) + 0.15·max(0, unique_z)
      + 0.15·hype + 0.10·repeats + 0.05·|sentiment|
```

A moment is emitted when `score >= threshold` **and** the bucket has at least
`minimum_messages` and `minimum_rate`, and the cooldown has elapsed.

### Median/MAD, not mean/std

This is the single most important design decision. With mean + standard
deviation, one viral burst inflates the baseline enough to hide the next burst
minutes later. Median and MAD stay anchored to normal chat:

```python
median = sorted(rates)[len(rates) // 2]
mad    = median(|rate - median|)
scale  = max(0.35, mad * 1.4826)   # 1.4826 ≈ normal-consistent MAD
```

The floor of `0.35` prevents a dead-quiet chat from producing infinite z-scores.

### Triggering inside the bucket

Waiting for a bucket to close costs up to 4 seconds of post-roll. The detector
re-evaluates on every message once `minimum_messages` is reached, so a rapid
burst triggers within ~1–2 seconds.

### Evidence

Every trigger stores `SpikeSignal` (all five components, rounded) plus the top
five messages, ranked by hype weight, with URLs penalised so spam doesn't win.
This is what lets a live clip say *why* it was cut, and it is what the Live
monitor renders as the spike feed.

---

## Watching the stream, not just chat

`live/content.py` analyses the newest slice of the ring buffer every
`content_interval` seconds and scores three independent signals:

| Signal | What it measures | Examples |
|---|---|---|
| `speech` | the transcribed window: hype phrases, exclamation marks, ALL CAPS, fast delivery, repetition, profanity, laughter | "NO WAY!", "I can't believe it", a shouted sentence |
| `audio` | peak loudness against the window's **own** baseline (dB jump) | a scream, a hit, an explosion, a mic drop |
| `action` | hard scene changes per second, scored on the densest 4-second burst | standing up, jumping, throwing something, a camera cut |

```
score = 0.62·speech + 0.55·audio + 0.45·action
```

The weights are close on purpose. A scream with no words, a wild sentence with
no movement and a silent action beat are each valid clips; correlation only
raises the score. Every trigger stores its reasons (`"hype language: no way"`,
`"loudness spike (+19 dB over the window)"`, `"5 cuts in 4s (motion burst)"`),
which is what the UI and the clip copy show.

### Speech is skipped when it would be fake

If the resolved transcriber is `mock` (no whisper, no API key), speech scoring
is disabled rather than inventing a viral line from a placeholder transcript.
Audio and action still run, so a keyless box still catches screams and visual
moments. The monitor stats expose this as `content.speech`.

### Fusion

Chat and content triggers within **12 seconds** of each other are the same beat:

* one moment, one clip — the start is extended to cover both windows,
* evidence is merged (chat z-score + speech/audio/action scores + the best
  messages from both),
* the score gets a small bonus for agreement,
* the moment's `origin` becomes `fused`, otherwise `chat`, `speech`, `audio` or
  `action`.

Distinct moments still respect the normal `cooldown` (45s by default), so a
wild two-minute segment is not cut twelve times.

### Options

| Option | Default | Meaning |
|---|---|---|
| `content_detection` | `true` | watch the stream, not only chat |
| `content_threshold` | `2.5` | score needed to trigger (lower = more clips) |
| `content_interval` | `18` | seconds between analysed windows |
| `speech_window` | `32` | seconds of buffer transcribed per analysis |

Analysing a 32-second window every 18 seconds costs one transcription call per
window. With `faster-whisper` on CPU that is a few seconds, which is why the
interval is not shorter.

```bash
# chat + speech + audio + action (default)
clipbait live "https://www.youtube.com/watch?v=..." --content-threshold 2.5

# chat only
clipbait live "https://www.youtube.com/watch?v=..." --no-content
```

---

## Rolling recorder

`live/recorder.py` runs ffmpeg with `-c copy -f segment` into 4-second MPEG-TS
files, keeping a CSV segment list. A watcher thread ingests new rows, records
each segment's cumulative start time, and prunes anything older than the
monitor's `buffer_seconds` (default 420 s, env fallback
`CLIPBAIT_LIVE_BUFFER_SECONDS`). The ring has to outlast content analysis: a
32 s window with scene detection and whisper can finish minutes after the audio
it scored, and a 180 s ring turns that normal lag into a skipped clip.

When a moment is captured:

1. `select_segments` picks every segment overlapping `[start, end]` and computes
   the offset into the first one.
2. The capture thread waits until the last needed segment is complete (bounded
   at 90 s so a dead stream cannot hang forever).
3. An ffconcat file is written and ffmpeg cuts the window, applying `-ss <offset>
   -t <length>` **after** `-i` (an output seek).

Stream copy is used for capture (cheap, accurate timestamps); the slice is
re-encoded once so every clip starts on a keyframe and has clean audio.

### Seek after `-i`, not before it

The seek must be an output option. An input seek (`-ss` before `-i`) on a concat
of TS segments lands the **video** on the nearest keyframe — up to a second
*after* the **audio** seek point — so the slice opens with audio that has no
matching picture. The render rebases each stream independently, so that offset is
frozen in and every burned caption sits that far off the lips. As an output seek,
ffmpeg decodes from the head of the already-trimmed window and applies one seek
point to both streams, keeping video and audio starting together.
`tests/test_live.py::test_hls_slice_seeks_after_input_so_av_stay_aligned` locks
this in.

`FileRecorder` implements the same interface over a local file on a synthetic
clock. That is what `demo://live` and the tests use, so the live pipeline is
exercisable without network access.

---

## Clock alignment

The subtle bug that breaks live clipping: the recorder and the chat source start
at different times. Resolving a stream URL can take 30 seconds; meanwhile the
chat connector has already been reading messages. If you stamp chat with
"seconds since the monitor began", every clip is late by that gap.

Clipbait uses one clock, and the recorder owns it:

```python
self.recorder = build_recorder(self.url, storage.live_dir(self.monitor_id))
self.recorder.start()                      # resolves, then launches ffmpeg
self.started_at = self.recorder.started_at  # stamped when ffmpeg starts
self.chat = resolve_chat_source(...)
self._chat_offset = max(0.0, self.chat.started - self.started_at)
```

`Recorder.started_at` is set the moment media capture begins, and HLS segments
are rebased onto it, so segment timestamps, chat time and content windows agree.
A restarted monitor gets a fresh clock and does not inherit stale elapsed time.

### Ring-buffer misses

A monitor cannot clip footage it never recorded. Chat can spike before the
first HLS segment exists, and the newest segment is still filling when a moment
ends. `LiveRunner._capture` handles both:

- clamp `start` up to `available_from` and `end` down to `available_until`
- wait up to 90s for the tail of the buffer to reach `end`
- skip cleanly when fewer than 4s of the moment survive, recording the reason
  in `moments.signal.capture_error` and publishing `capture_skipped`
- never log a stack trace for an ordinary buffer miss

That is the difference between a monitor that dies on the first early spike and
one that keeps recording until a moment it can actually cut.

---

## Chat transports

| URL | Transport | Key | Notes |
|---|---|---|---|
| `https://youtube.com/watch?v=…`, `/live/…`, `youtu.be/…` | `pytchat` | none | `pip install 'clipbait[live]'` |
| same | `pytchat` (optional) | none | used when the extra is installed |
| same | InnerTube (`innertube`) | none | keyless default when pytchat is absent; reads the public web chat endpoint |
| same | YouTube Data API | `YOUTUBE_API_KEY` | used when `chat_provider=api`, or as `auto` fallback if pytchat is missing and a key exists |
| `https://twitch.tv/<channel>` | anonymous IRC over TLS | none | `PASS oauth:anonymous`, PING/PONG handled |
| `demo://live` or `demo` | synthetic | none | deterministic spikes at ~20 s, 75 s, 130 s |
| JSONL file | replay | none | `JsonlChatSource` for recorded chat, `t`/`author`/`text` per line |

All sources yield `ChatMessage(t, author, text, kind)`. Connectors own their
reconnection loops; the orchestrator only consumes a clean stream.

Limitations: Kick chat is not implemented (Kick VODs work through yt-dlp).
YouTube's official API path only sees public streams and has quota costs.

---

## Demo mode

```bash
clipbait live demo --threshold 2.5 --region IN
```

`demo://live` does three things:

1. Generates a 240-second synthetic stream once (cached as
   `data/live/<monitor_id>/demo_source.mp4`, reused afterwards).
2. Emits calm chat punctuated by scripted hype bursts at 20 s, 75 s and 130 s.
3. Runs the real detector, recorder, capturer, transcriber, reframer, captioner
   and packager.

If a render takes a while, that is real work — it is the production path with a
fake camera. The UI labels the monitor as a demo.

Use it to verify an install, to check an encoder change, or to demonstrate the
product without going live.

---

## Channel look: effects, music, meme SFX

Three optional touches make a channel's Shorts feel produced rather than plain.
All three are safe-by-default (`none` / off), so a clip is unchanged until the
operator asks for them, and they are exposed identically on `live` and
`autopilot` via the shared flags.

| Flag | Default | What it does |
|---|---|---|
| `--effect none\|stopmotion\|punch` | `none` | `stopmotion` drops the picture to ~12 updates/s and resamples back: frame-hold judder that reads as stop-motion. `punch` crops 8% and scales back for a constant subtle punch-in. Captions are burned **after** the effect, so the text never judders or zooms. |
| `--music [TRACK]` | off | Mixes a music bed quietly under the speech. With no name, one track from `CLIPBAIT_MUSIC_DIR` is chosen per clip id, so clips vary without an operator picking each one; a name resolves against the working directory then that folder. |
| `--music-volume V` | `0.16` | Music level (`0`–`1`). |
| `--no-meme-sfx` | SFX on | Play a short sound effect (from `CLIPBAIT_MEME_SFX_DIR`) over each meme cutaway, so a cutaway lands with a punch instead of silently. |

```bash
clipbait live "https://www.twitch.tv/somechannel" \
  --effect stopmotion --music hype.mp3 --music-volume 0.2
```

Every source is summed with `amix ... normalize=0`, so the speech stays at full
level rather than being halved by the mix, and a mild final `volume` guards
against the summed peaks clipping. A silent source still gets a bed: the music
becomes the single `[a]` stream, so `-map [a]` and the encoder never change.

Meme inserts are library-backed and can be **images as well as GIFs/video**:
`prepare_insert` loops a still jpg/png/webp with `-loop 1`, an animated source
with `-stream_loop -1`, and fits either whole on a blurred fill, so a static meme
is as usable as an animated one. See the meme providers and env keys in
[`README.md`](../README.md) (`CLIPBAIT_MEME_*`).

---

## Running a monitor

### UI

**Live → New monitor**: paste a YouTube/Twitch URL, choose threshold, pre/post
roll, region, platform, caption preset, chat overlay. The detail page streams
`stats`, `spike`, `content` and `spike_fused` events over SSE, shows the content
signals (speech / loudness / action) for each moment, and lists clips as they
finish.

### CLI

```bash
clipbait live "https://www.twitch.tv/somechannel" \
  --region BR --platform reels --threshold 2.5 --preset hormozi
```

`Ctrl-C` stops the monitor and the recorder. `--no-clip` keeps detection on but
skips rendering (useful to tune a threshold without burning CPU).
`--no-content` disables on-stream detection; `--content-threshold` tunes it
independently of the chat threshold.

### API

```bash
curl -s localhost:8000/api/v1/live/monitors \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=…",
       "options":{"spike_threshold":2.5,"region":"IN","platform":"reels"}}'

curl -N localhost:8000/api/v1/live/monitors/<id>/events   # SSE
```

Monitors are in-memory runners backed by database rows. Restarting the server
stops active monitors; the rows, chat history and moments remain for inspection.

### Unattended

`clipbait autopilot` batches: it renders clips during a round and hands them to
the publisher when the round ends. To keep watching a stream on its own,
`scripts/supervise_live.py` pins one autopilot to a named streamer's *current*
live room — it re-resolves the room on each check, restarts the child when the
room changes or the child exits, and stops it when the streamer goes offline.

```bash
.venv/bin/python scripts/supervise_live.py \
  --match "kai cenat" --query "gta 6" --platform shorts \
  --live --now --minutes 30 --threshold 2.0 --content-threshold 1.3 --min-score 20
```

With `--live`, each clip is posted **the moment it renders**, not at the round's
end (`MonitorOptions.auto_publish`): the round-end publish stays on as a safety
net and a retry for anything that failed. Uploads are serialized so clips that
land close together cannot race the provider's upload, and every post goes to the
destination the run was set up for (`--platform`), never the money layer's
recommended network — so a run only ever hits a channel that is actually
connected.

Run it as a systemd user service so it survives logout and reboot; the repo ships
a template at [`docs/clipbait-autopilot.service`](clipbait-autopilot.service).

### Across many streamers (roster)

`supervise_live.py` watches one person. To watch whoever is biggest *right now*,
`scripts/supervise_roster.py` searches the live filter for a set of topics (and
optionally a list of named streamers), ranks the rooms by concurrent viewers, and
keeps the autopilot on the top `--concurrency` of them, rotating as streams end:

```bash
.venv/bin/python scripts/supervise_roster.py \
  --queries "gta 6,just chatting,fortnite,valorant,music" \
  --priority "ishowspeed" \
  --min-viewers 3000 --concurrency 1 \
  --platform shorts --live --now --threshold 2.0 --content-threshold 1.3 --min-score 20
```

`--priority` names streamers to favour: a priority channel sorts ahead of every
non-priority room **even when it has fewer viewers**, so "take as many clips of
this person as possible" holds while they are live, and the roster still fills
with the biggest other rooms when they are not. Priority names are also searched,
so a favoured streamer is discovered even when no topic query matches their room.

`--channels` watches a fixed channel's `/live` room directly, which a text search
cannot do when a streamer's live titles omit their own name (iShowSpeed streams
"EXPLORING VICE CITY", not "iShowSpeed"). Offline channels drop out silently, so
the roster still fills from the normal queries:

```bash
  --channels "https://www.youtube.com/@IShowSpeed"
```

### Disk never fills

A roster never stops, and its raw material — HLS segments, fetched sources,
per-clip windows — grows until the recorder can no longer write. `--prune-hours`
(default 6) runs the same reclaim the `clipbait cleanup` command exposes, once per
cycle, and it is safe by construction:

* a **monitor** directory is pruned only when nothing inside it has been written
  for the window (the recorder appends to one monitor at a time, so a quiet one is
  finished);
* a **project source** is pruned only when the project is terminal
  (`completed`/`failed`) *and* idle — a long transcription leaves the file
  untouched for hours while it is still needed to render the clips.

Preview it any time without deleting:

```bash
.venv/bin/clipbait cleanup              # list what would go
.venv/bin/clipbait cleanup --apply      # delete it
.venv/bin/clipbait cleanup --older-than 12 --apply
```

Two policies keep a roster run honest and safe, and both are off/blank by default:

* **Mature rooms.** `--mature-terms` (on top of a built-in adult list) plus
  `--mature-policy skip` keeps explicit streams out of the channel. The upload
  API cannot set YouTube's age gate — that stays a Studio decision — so the only
  reliable lever here is not to clip it.
* **Rights.** `--rights-file` + `--require-rights` restrict posting to channels
  you have cleared. Clipping someone else's stream is not automatically
  monetizable; keep the top of the channel only for streamers you are allowed to.

A 4-core box should stay at `--concurrency 1`: each room is its own autopilot
process with its own model and renders. The service template is at
[`docs/clipbait-roster.service`](clipbait-roster.service).

---

## Tuning

Start with the defaults, then adjust one variable at a time:

| Symptom | Change |
|---|---|
| Too many clips from normal chatter | raise `spike_threshold` (3.0 → 4.0) |
| Missing real moments | lower threshold (3.0 → 2.2) and/or `minimum_messages` |
| Clips start too late | increase `pre_roll` (25 → 35) |
| Clips cut off the payoff | increase `post_roll` (20 → 30) |
| Same moment clipped repeatedly | increase `cooldown` (45 → 90) |
| Chat is slow but the moment is visual | content detection already covers it; lower `content_threshold` |
| Too many clips from normal speech | raise `content_threshold` (2.5 → 3.5) |
| Missing a viral line before chat reacts | lower `content_threshold`, keep `speech_window` ≥ 24 |
| Rendering lags behind a busy stream | raise `content_interval` (18 → 30) |
| Speech detection never fires | `content.speech` is false: install faster-whisper or add an API key |
| Buffer overrun / no segments found | raise `CLIPBAIT_LIVE_BUFFER_SECONDS` |
| Disk filling up | lower buffer seconds and prune old projects |

`SpikeConfig.minimum_rate` (messages/second) is the second gate: it stops a
quiet chat from triggering on a handful of messages that happen to be a z-score
outlier.

## Operational notes

* **Disk** — a 420 s ring at 720p is roughly 150–350 MB per monitor. Moments and
  rendered clips add more. `DELETE /projects/{id}` removes a project's files.
* **CPU** — capture is stream-copy (cheap); slicing and rendering are not.
  Concurrent monitors multiply render load. Raise
  `CLIPBAIT_RENDER_CONCURRENCY` only if the box can take it.
* **One process** — jobs and monitors are threads inside the API process. Run a
  single uvicorn worker. Horizontal scaling needs the queue swap described in
  [`ARCHITECTURE.md`](ARCHITECTURE.md).
* **Privacy** — chat is personal data. It stays in your local SQLite file and is
  pruned after `max(1800, buffer_seconds × 4)` seconds. Export or delete it
  according to your own obligations.
* **Rights** — clip only streams you own or are permitted to reuse. The tool
  does not grant rights to the underlying content.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `pytchat is not installed` | forcing `chat_provider=pytchat` without the extra | `pip install 'clipbait[live]'`, or use `auto` / `innertube` |
| `this YouTube video has no readable live chat` | stream ended, chat disabled, or the page changed | verify the stream is live and public; set `YOUTUBE_API_KEY` for the official API path |
| `could not resolve the live stream URL ...` | yt-dlp could not see a live format | confirm the stream is live and public; the message includes yt-dlp's reason |
| `this video has no active live chat` | stream ended or chat disabled | verify the stream is live |
| `cannot extract a YouTube video id` | URL form not recognised | use a canonical watch/live URL |
| `requested range is older than the ring buffer` | capture lagged past the buffer | raise `CLIPBAIT_LIVE_BUFFER_SECONDS` or reduce pre-roll |
| Monitor stuck in `starting` | stream resolve is slow or blocked | check yt-dlp can resolve the URL; see logs |
| No spikes ever | threshold too high or chat too quiet | run `clipbait live demo` first, then lower threshold |
| Clips are silent / no captions | audio missing in the stream | check the source; `mock` transcripts are flagged in the UI |