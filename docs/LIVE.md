# Live clipping

Most clippers are VOD tools with a live URL bolted on. Clipbait treats live
chat as the highlight signal: a stream's audience tells you where the moment is,
in real time, and it does it for free.

```
stream ──▶ rolling recorder ─────────────────────────────┐
              (last N minutes of .ts segments)           │
chat ──▶ spike detector ──▶ moment ──▶ capture thread ───┴─▶ slice.mp4
                                                             │
                                              transcribe ──▶ reframe ──▶
                                              captions   ──▶ packaging ─▶ clip
```

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

## Rolling recorder

`live/recorder.py` runs ffmpeg with `-c copy -f segment` into 4-second MPEG-TS
files, keeping a CSV segment list. A watcher thread ingests new rows, records
each segment's cumulative start time, and prunes anything older than
`CLIPBAIT_LIVE_BUFFER_SECONDS` (default 180 s).

When a moment is captured:

1. `select_segments` picks every segment overlapping `[start, end]` and computes
   the offset into the first one.
2. The capture thread waits until the last needed segment is complete (bounded
   at 90 s so a dead stream cannot hang forever).
3. An ffconcat file is written and ffmpeg cuts with `-ss <offset> -t <length>`,
   re-encoding to H.264/AAC with `+faststart`.

Stream copy is used for capture (cheap, accurate timestamps); the slice is
re-encoded once so every clip starts on a keyframe and has clean audio.

`FileRecorder` implements the same interface over a local file on a synthetic
clock. That is what `demo://live` and the tests use, so the live pipeline is
exercisable without network access.

---

## Clock alignment

The subtle bug that breaks live clipping: the recorder and the chat source start
at different times. Resolving a stream URL can take 30 seconds; meanwhile the
chat connector has already been reading messages. If you stamp chat with
"seconds since the monitor began", every clip is late by that gap.

Clipbait uses one clock:

```python
self.started_at = time.monotonic()
self.recorder.start()
self.chat = resolve_chat_source(...)
self._chat_offset = max(0.0, self.chat.started - self.started_at)
```

Each message is rebased by `_chat_offset` before it reaches the detector, so
chat time and video time agree. The clock is reset at start, which also means a
restarted monitor does not inherit stale elapsed time.

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

## Running a monitor

### UI

**Live → New monitor**: paste a YouTube/Twitch URL, choose threshold, pre/post
roll, region, platform, caption preset, chat overlay. The detail page streams
`stats` and `spike` events over SSE and shows clips as they finish.

### CLI

```bash
clipbait live "https://www.twitch.tv/somechannel" \
  --region BR --platform reels --threshold 2.5 --preset hormozi
```

`Ctrl-C` stops the monitor and the recorder. `--no-clip` keeps detection on but
skips rendering (useful to tune a threshold without burning CPU).

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
| Chat is slow but the moment is visual | use a lower threshold and rely on `minimum_rate` |
| Buffer overrun / no segments found | raise `CLIPBAIT_LIVE_BUFFER_SECONDS` |
| Disk filling up | lower buffer seconds and prune old projects |

`SpikeConfig.minimum_rate` (messages/second) is the second gate: it stops a
quiet chat from triggering on a handful of messages that happen to be a z-score
outlier.

## Operational notes

* **Disk** — a 180 s ring at 720p is roughly 60–150 MB per monitor. Moments and
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