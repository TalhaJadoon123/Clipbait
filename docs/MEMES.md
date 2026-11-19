# Meme video inserts

A short b-roll cutaway **inside** a clip: the picture cuts to a generated meme
for a moment, then cuts back. Off by default — it changes the footage, not just
the copy. Enable it per project with **Meme video inserts** in the UI or
`meme_inserts: true` in the API request.

## The two rules

1. **Only if needed.** A cutaway is planned only where the transcript has a
   natural seam or a strong emotional signal. A clip with neither gets no
   insert. An empty plan is the common, correct outcome.
2. **Audio and captions never drift.** The insert replaces the **video only**.
   The original audio keeps playing underneath and word-level captions are
   burned on top from the same timings. Nothing is re-timed, so an insert can
   never desync the captions.

## When an insert is planned

`src/clipbait/memes/plan.py` produces the plan. Two kinds of candidate:

- **Gap** — a span of dead air between two words of at least `min_gap` (0.65s).
  Score grows with the gap length, capped so a long silence cannot outrank a
  genuine signal.
- **Signal** — a window of text (`signal_window`, 6s) whose detected signals sum
  to at least `MIN_SIGNAL_SCORE` (3.0) via `SIGNAL_WEIGHTS`. Only signals worth
  interrupting for count; purely informational ones (`number`, `hack`) are
  deliberately weighted at zero.

Signals come from the same detector the copy formats use
(`virality/memes.py: detect_signals`), so the picture and the packaging agree on
what kind of moment it is.

Candidates are then:

- discarded within `head_skip` (1.5s) or `tail_skip` (1.0s) of the clip edges,
  so a clip never opens or closes on b-roll;
- thinned so two inserts sit at least `min_spacing` (8s) apart;
- capped at `max_inserts` (default 2), keeping the highest-scoring candidates.

Per-project knobs (`ProjectOptions`): `meme_inserts`, `meme_insert_seconds`
(0.8–5.0), `meme_max_inserts` (1–6).

## Where the video comes from

`src/clipbait/memes/providers.py` has two providers behind one interface:

| Provider | Selected when | What it does |
|---|---|---|
| `ApiMemeProvider` | `CLIPBAIT_MEME_API_URL` is set (or provider `api`) | POSTs a prompt to a hosted text-to-video endpoint and downloads the result |
| `FixtureMemeProvider` | no API URL (or provider `fixture`) | Renders a labelled placeholder card locally with ffmpeg |

`CLIPBAIT_MEME_PROVIDER=none` disables inserts entirely. The API client is
deliberately forgiving because provider schemas differ: it accepts a
synchronous body containing a URL, or an id it polls at `<url>/<id>`, and it
treats any failure as "no insert". Downloaded URLs pass the same SSRF guard as
any other user-supplied URL, and there is a hard 120 MB ceiling on an asset.

The fixture provider exists for exactly the reason the mock transcriber does: the
feature should be usable and testable with **no API key at all**. It always
renders a clearly synthetic card.

## How it renders

`render/ffmpeg.py` composites the insert as an ffmpeg `overlay` whose window is
gated by `enable='between(t,start,end)'`, applied **before** the `ass` caption
pass. The insert's PTS is shifted onto the clip timeline
(`setpts=PTS-STARTPTS+start/TB`) so the window finds its frames. Because the
overlay sits under the caption layer and never touches the audio stream, the
spoken audio and its captions run continuously underneath.

`memes/assets.py: prepare_insert` normalises whatever a provider returned
(arbitrary size, frame rate, pixel format, audio) into one exact shape: the clip
resolution, the clip frame rate, `yuv420p`, silent, and trimmed to the window
length (looping the source if it is shorter). A missing or too-short asset is
skipped, so a bad insert degrades to "no meme" rather than a failed render.

## Inspecting the result

Each rendered clip persists `meta.inserts` (start, end, reason, signals, source,
prompt, path, provider) and `meta.insert_count`. The raw generated asset is
cached under `<data>/memes/` keyed by a hash of the prompt and shape, so repeated
clips reuse it; the normalised per-clip asset lives under
`<data>/projects/<id>/clips/<clip_id>/inserts/`.
