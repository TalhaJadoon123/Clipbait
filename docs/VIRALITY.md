# Region & meme intelligence

Clipping is solved. Distribution is not.

The same 40-second story is a different product in the US, India and Brazil.
The hook that reads confident in Ohio reads arrogant in Seoul; the hashtag that
travels in Manila is dead weight in Warsaw. Clipbait treats that as **data**, not
vibes: 26 market profiles and 26 evergreen meme structures, matched to content
signals and constrained by platform limits.

Everything here is editable without touching prompts or retraining anything.

---

## The packaging pipeline

```
transcript window
      │
      ├─ detect_signals() ──▶ {"failure", "revelation", "contrast", "awe"}
      │
region ──── select_memes() ──▶ ranked meme formats (region + platform + risk)
      │
      └── build_variants() ──▶ organic · meme(s) · regional CTA
                                   │
platform limits ───────────────────┤
                                   ▼
                            forecast() ──▶ 0–100 with component breakdown
```

`build_variants` returns ranked alternatives, not one guess:

| Kind | What it is |
|---|---|
| `organic` | the literal line from the clip, lightly title-cased — safest |
| `meme` | the line shaped through a matched meme structure |
| `cta` | the line plus the market's native call to action |

Every variant carries `title`, `hook`, `description`, `hashtags`, `cta`,
`rationale`, and for memes the full format object. The operator picks one in the
UI; the choice is stored as `meta.selected_variant` and used for the render and
the file's packaging.

---

## Market profiles

`src/clipbait/virality/regions.py` defines a frozen `RegionProfile`:

```python
RegionProfile(
    code, name, flag, languages, humor,
    emoji_density,        # 0-3, controls caption emoji and hooks
    caption_preset,       # default caption look for this market
    hashtags,             # evergreen tags, never trending spam
    cta,                  # native call to action
    posting_windows,      # local-time ranges
    platform_bias,        # where this market actually watches
    slang=(),             # terms that make copy feel local
    notes="",             # the editing brief in one or two lines
    avoid=(),             # topics that damage brands in this market
)
```

Shipped markets:

`AE AR AU BD BR CA DE ES FR ID IN JP KR MX NG NL PH PK PL SA TH TR UK US VN ZA`

Examples:

| Code | Market | Humour register | Preset | Windows (local) |
|---|---|---|---|---|
| `US` | United States | punchy, confident, direct | `hormozi` | 07–09, 12–13, 18–22 |
| `IN` | India | high-energy, story-first | `lime` | 08–10, 13–15, 20–23 |
| `BR` | Brazil | loud, funny, expressive | `lime` | 11–13, 17–19, 20–23 |
| `DE` | Germany | precise, dry, proof-first | `clean` | 07–09, 12–14, 18–21 |
| `PH` | Philippines | warm, dramatic, relational | `hormozi` | 11–13, 17–19, 21–24 |
| `NG` | Nigeria | playful, assertive, faith-aware | `default` | 07–09, 12–14, 19–22 |

The `avoid` list is not decoration. It is the difference between a campaign and
a crisis: political endorsements and financial guarantees in `US`, caste and
communal framing in `IN`, historical grievances in `PL`.

### Why posting windows are ranges

They are **local-time heuristics**, not a scheduler. Clipbait prints the best
window and the UI shows all of them; you still decide when to post. Treat them
as a prior that your own analytics should override.

### Overriding a market without a fork

Anything in a profile can be replaced at runtime with one JSON env var:

```bash
export CLIPBAIT_REGION_OVERRIDES='{
  "IN": {"cta": "Comment GUIDE and I will send the template",
         "hashtags": ["#shorts", "#india", "#businessideas"],
         "posting_windows": ["09:00-11:00", "20:00-23:00"]}
}'
```

Rules:

* keys are region codes, values are partial profiles;
* `code`, `name` and `flag` are not overridable (they are identity, not policy);
* list fields accept a JSON array or a comma-separated string;
* invalid JSON, unknown regions and unknown fields are ignored with a log line —
  a bad override must never take the product down.

To add a genuinely new market, append a `RegionProfile` to `REGIONS`.

---

## Meme formats

`src/clipbait/virality/memes.py` defines a `MemeFormat`:

```python
MemeFormat(
    id, name,
    pattern,      # "{subject}" / "{claim}" / "{twist}" placeholders
    signals,      # emotional signals this format requires
    regions,      # empty = global
    platforms,
    risk="low",   # low | medium
    example="",
    note="",
)
```

26 structures ship, including `POV`, `Nobody:`, `Tell me why`, `Wait for it`,
`The way …`, `Me when …`, `Expectation vs reality`, `Cooked`, `Rent free`,
`Main character`, `No way`, `Delulu`, `Sana all` (PH), `Grabe` (PH), `Eish…`
(ZA), `Wahala` (NG), `Jugaad` (IN), `Scene` (IN), `Neta` (IN), `O jeito…` (BR),
`Mashallah` (MENA), `Krasno` (PL/RU-adjacent), `Yabai` (JP), `Daebak` (KR),
`Xieu` (VN), `555` (TH).

Two hard rules:

1. **Patterns are structures, not assets.** No copyrighted image, audio or
   template is bundled or generated. This is hook scaffolding.
2. **`medium` risk is opt-in.** Formats that rely on mockery, accent play or
   second-hand embarrassment are filtered unless `allow_medium_risk=True`
   (UI toggle, API option, CLI off by default).

A `{twist}` format is skipped when the clip has no `reason` — an empty twist is
worse than no joke.

### Signal detection

`detect_signals` is regex-based and deliberately plain:

| Signal | Example trigger |
|---|---|
| `story` | "I was…", "we built…" **plus** a narrative marker (then/after/finally) |
| `confession` | "honestly", "I never told" |
| `relatable` | "you know when", "most people" |
| `surprise` | "no way", "insane", "unbelievable" |
| `revelation` | "the secret", "turns out", "here's why" |
| `number` | "300 videos", "40%" |
| `failure` | "failed", "lost", "fired", "broke" |
| `contrast` | "but", "however", "until" |
| `awe` | "mind blown", "unreal" |
| `hustle` | "client", "revenue", "sales" |
| `conflict`, `opinion`, `confidence`, `hack`, `value`, `praise`, `wholesome`, `envy`, `absurd`, `story_peak` | … |

`story` is the one signal that requires an arc, not just a pronoun. Without that
guard every sentence beginning with "I" claims to be a story.

### Selection

```
score = 2.0 × |signals ∩ meme.signals|
      + 1.2 if meme is native to the region
      + 0.4 if the format is global
```

Filters run before scoring: risk, region membership and platform support. Result
is deterministic and testable — no model call.

---

## Platform limits

`platform_limits()` enforces real constraints before anything is emitted:

| Platform | Title | Caption | Hashtags |
|---|---|---|---|
| `tiktok` | 100 | 2200 | 6 |
| `reels` | 125 | 2200 | 8 |
| `shorts` | 100 | 5000 | 3 |
| `linkedin` | 200 | 3000 | 5 |
| `x` | 120 | 280 | 2 |

Titles are also capped at 76 characters in practice, because a title that gets
truncated in-feed loses the punchline. `_trim` cuts on a word boundary and never
leaves a dangling comma.

Hashtags are built from the region's evergreen set plus clip keywords, with a
junk list (`right`, `thing`, `actually`, `video`, …) removed. Generic filler tags
dilute reach; they are not free.

---

## The forecast

`forecast()` produces an explainable 0–100 estimate with components:

```
retention     = 100 − min(45, |duration − 38| × 1.1)
length_penalty= 0 inside 22–62s, else min(25, |duration − 42| × 0.5)

regional_fit  = 55 + 18 (native slang present) + 8 (emoji density ≥ 2 and emoji used)
                  + 10 (platform is primary in this market)

meme_fit      = match × 100, −10 for medium risk
trend_fit     = min(100, 35 + 18 × trend keyword hits)

overall = 0.30·hook + 0.20·emotion + 0.15·value + 0.15·share
        + 0.08·retention + 0.05·regional_fit + 0.04·meme_fit
        + 0.03·trend_fit − length_penalty
```

Returned with `signals`, human-readable `reasons`, the market's `best_time` and
an explicit disclaimer. The weights say what the product believes: the hook is
worth more than the trend, and duration is a tax you pay for a slow setup.

> This is a directional editing heuristic. It is not a promise of views, reach
> or revenue, and it is not trained on your account.

---

## Caption looks

Caption presets interact with region. `emoji_density` and `caption_preset` are
part of the market profile, so `IN` defaults to high-energy `lime` while `DE`
defaults to the restrained `clean`.

| Preset | Character |
|---|---|
| `default` | Anton caps, lime active word |
| `hormozi` | words appear as spoken, yellow active word |
| `pill` | purple box behind the active word |
| `lime` | lime box behind the active word |
| `oneword` | one large word at a time |
| `cine` | wider tracking, softer shadow |
| `clean` | no outline, soft shadow, sentence case |

Changing the preset triggers a re-render; packaging copy is unaffected.

---

## Using it

### UI

**Playbooks** shows all markets (expandable briefs), all meme formats (search and
region filter), caption presets and detected capabilities. **New clip** has the
region/platform pickers and a live preview of the market brief, so you see the
CTA and posting windows before you spend a render.

### CLI

```bash
clipbait process ./podcast.mp4 \
  --region IN --platform reels --preset lime --clips 6

clipbait process ./talk.mp4 --region DE --platform linkedin --no-memes
```

`--no-memes` keeps only the organic and CTA variants — the right call for
regulated or brand-sensitive accounts.

### API

```bash
curl -s localhost:8000/api/v1/meta | jq '.regions[0], .memes[0]'
curl -s -X POST localhost:8000/api/v1/clips/<clip_id>/variants/sana_all
```

`/meta` is the catalog: regions, memes, presets, platforms, capabilities and the
prompt version, so a client never hardcodes the data.

---

## Extending

**New market** — append to `REGIONS` in `regions.py`. Required fields are
positional; everything after `platform_bias` is optional.

**New meme format** — append to `MEMES` in `memes.py`:

```python
MemeFormat(
    "quiet_part", "The quiet part", "The quiet part out loud: {claim}",
    ("confession", "revelation"), (), ("tiktok", "reels", "shorts"),
    example="The quiet part out loud: nobody reads the docs",
)
```

It becomes selectable immediately — API, UI and pipeline all read the same tuple.
Add a test if the format has a special rule (region lock, risk, pattern
placeholder), because the selection logic is deterministic and cheap to pin.

**New trend keywords** — extend `TREND_KEYWORDS` in `packaging.py`. These are
deliberately generic technology/creator terms, not a live trend feed. If you want
live trends, wire your own source into `forecast` rather than hardcoding them.

**New platform** — add a row to `platform_limits` **and** the `Literal` in
`schema.ProjectOptions.platform`, then add it to `PLATFORMS` in `api/app.py` so
`/meta` advertises it.

---

## What this is not

* Not a cultural oracle. Profiles are averages, and averages are wrong about
  individuals. Read the `notes` and the `avoid` list before publishing.
* Not a translation layer. Language codes tell the transcriber what to expect;
  they do not localise a clip.
* Not a guarantee of virality. A forecast is a checklist with numbers attached.
* Not an excuse to impersonate a culture. Using `Sana all` in Manila is native;
  pasting it onto unrelated content is cosplay, and audiences can tell.

Use the playbooks as a starting brief, then let your own account data correct
them.