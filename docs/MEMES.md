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

`src/clipbait/memes/providers.py` has three providers behind one interface:

| Provider | Selected when | What it does |
|---|---|---|
| `HttpMemeProvider` (`search`) | `CLIPBAIT_MEME_SEARCH_URL` is set (or provider `search`) | Searches a meme library you already have access to and downloads a matching video |
| `ApiMemeProvider` (`api`) | `CLIPBAIT_MEME_API_URL` is set (or provider `api`) | POSTs a prompt to a hosted text-to-video endpoint and downloads the result |
| `FixtureMemeProvider` (`fixture`) | nothing configured (or provider `fixture`) | Renders a labelled placeholder card locally with ffmpeg |

`CLIPBAIT_MEME_PROVIDER=none` disables inserts entirely. `auto` prefers a real
meme library, then generation, then the local fixture.

The clients are deliberately forgiving because provider schemas differ. Search
results are read through configurable dot paths, and candidates are tried in
order, so an image-only result or a dead CDN falls through to the next video. A
generation response may be synchronous (a URL in the body) or asynchronous (an
id polled at `<url>/<id>`). Every failure becomes "no insert". Downloaded URLs
pass the same SSRF guard as any other user-supplied URL, and there is a hard
120 MB ceiling on an asset.

The fixture provider exists for exactly the reason the mock transcriber does: the
feature should be usable and testable with **no API key at all**. It always
renders a clearly synthetic card.

## Search API contract

Any JSON search endpoint works — Giphy, Tenor, or your own catalogue. Point
`CLIPBAIT_MEME_SEARCH_URL` at it and describe the response shape:

```bash
CLIPBAIT_MEME_SEARCH_URL="https://api.example.com/v1/memes/search"
CLIPBAIT_MEME_SEARCH_KEY="..."             # optional
CLIPBAIT_MEME_SEARCH_AUTH="bearer"         # bearer | header | query | none
CLIPBAIT_MEME_SEARCH_RESULTS="results"     # dot path to the result list
CLIPBAIT_MEME_SEARCH_VIDEO="url"           # dot path to a video URL per result
```

The request is `GET <url>?q=<query>&limit=8` (the query is the insert prompt,
reduced to its strongest keywords). Auth styles:

| `CLIPBAIT_MEME_SEARCH_AUTH` | What is sent |
|---|---|
| `bearer` (default) | `Authorization: Bearer <key>` |
| `header` | `<CLIPBAIT_MEME_SEARCH_HEADER>: <key>` |
| `query` | `<CLIPBAIT_MEME_SEARCH_KEY_PARAM>=<key>` in the query string |
| `none` | nothing |

If the URL contains `{query}`, the query is substituted there instead of in the
`q` parameter.

Expected response (dot paths above resolve to the list and to each video URL):

```json
{
  "results": [
    {"id": "abc", "url": "https://cdn.example.com/abc.mp4", "title": "shocked"},
    {"id": "def", "url": "https://cdn.example.com/def.mp4", "title": "no way"}
  ]
}
```

Nested shapes work too. For Tenor:

```bash
CLIPBAIT_MEME_SEARCH_RESULTS="results"
CLIPBAIT_MEME_SEARCH_VIDEO="media_formats.mp4.url"
```

For Giphy:

```bash
CLIPBAIT_MEME_SEARCH_URL="https://api.giphy.com/v1/gifs/search"
CLIPBAIT_MEME_SEARCH_AUTH="query"
CLIPBAIT_MEME_SEARCH_RESULTS="data"
CLIPBAIT_MEME_SEARCH_VIDEO="images.hd.mp4,images.original.mp4"
CLIPBAIT_MEME_SEARCH_RATING="pg-13"
```

`CLIPBAIT_MEME_SEARCH_VIDEO` may name **several** dot paths, best-first. Each
result is probed left to right and the first path that yields a URL wins, so a
result that happens to carry an HD render contributes it while one that does not
falls back to its original. That matters for quality: Giphy's default asset is a
~200px thumbnail, and the cutaway fills the frame, so prefer the largest source
a result actually has.

Every value on the video path may also be an object containing `url` or `mp4`;
the provider unwraps it. If a result has no usable URL, the next one is tried.
The first video that downloads wins; none means "no insert", never a broken
clip.

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

The meme is scaled to **fit** the frame (never cropped) and centred over a
blurred, slightly darkened copy of itself that fills the frame. Cover-cropping a
small landscape GIF into a 9:16 clip would both blow it up several times and
slice off most of its width — unreadable — so the whole meme is kept and the
blurred fill hides the low source resolution. A phone-shaped meme simply fills
the frame with no visible fill.

## Inspecting the result

Each rendered clip persists `meta.inserts` (start, end, reason, signals, source,
prompt, path, provider) and `meta.insert_count`. The raw generated asset is
cached under `<data>/memes/` keyed by a hash of the prompt and shape, so repeated
clips reuse it; the normalised per-clip asset lives under
`<data>/projects/<id>/clips/<clip_id>/inserts/`.
