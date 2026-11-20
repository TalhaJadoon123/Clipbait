# Money queue

A rendered clip is not an asset until it is posted. The money layer answers two
questions in one place: **what is this clip likely to earn**, and **where should
it go**. It is deliberately conservative, because an "expected earnings" number
that flatters the operator is worse than no number at all.

```
ready clips ──▶ forecast score ──▶ view distribution ──▶ platform RPM ──▶ USD range
                     │                    │                    │
                     │                    │                    └─ region ad value
                     │                    └─ lognormal, platform-specific sigma
                     └─ 0-100, from transcript + metadata (docs/VIRALITY.md)

ranked queue ──▶ best platform per clip ──▶ export bundle (mp4 + copy + metadata)
```

---

## The estimate

`virality/payout.py` is pure math. Every clip gets a `PayoutReport` covering all
five destinations, sorted by expected USD.

1. **Forecast → percentile.** The clip's forecast score (0–100) is mapped to a
   percentile of that platform's view distribution. 50 is an average clip for
   that platform, 90 is a hit.
2. **Percentile → views.** Read off a lognormal. Median views and sigma are
   per platform, because the distributions genuinely differ:

   | Platform | Median views | Sigma | Sweet spot | RPM (mid) |
   |---|---|---|---|---|
   | TikTok | 1,200 | 1.15 | 34s | $0.45 |
   | Reels | 900 | 1.10 | 36s | $0.55 |
   | Shorts | 700 | 1.10 | 42s | $0.16 |
   | X | 400 | 1.05 | 28s | $0.30 |
   | LinkedIn | 220 | 0.95 | 75s | $3.20 |

3. **Views → USD.** `views / 1000 × RPM × region_factor`. The region factor is
   the advertiser value of an impression relative to the US: `US=1.00`,
   `UK=0.88`, `DE=0.78`, `BR=0.32`, `IN=0.24`, `PK=0.22`. The same million views
   is worth roughly five times more in the US than in a low-CPM market.

The range you see is the 20th–80th percentile of that distribution, not
`±10%`. Clip views are fat-tailed; a narrow range would be a lie.

### Why the best platform changes

Sigma is what makes this more than a constant ranking. TikTok's fat tail means a
likely hit can explode; LinkedIn's narrow, follower-driven distribution means a
mid clip earns more there because the RPM is seven times higher. Duration acts
as a second term: a 75-second clip fits LinkedIn's sweet spot and misses
TikTok's.

So the queue recommends **the best destination for that specific clip**, not
the project's default platform. Examples:

* score 80, 20s → TikTok (fast, high-reach clip)
* score 80, 75s → LinkedIn (RPM wins; TikTok loses reach on a long clip)
* score 40, 38s → LinkedIn or TikTok, effectively a tie and both small

The recommendation is one line of rationale, and the full `estimates` array is
in the API response so an operator can override it with evidence.

> Estimates are directional, from public RPM medians and a heuristic forecast.
> Never present them as guaranteed revenue.

---

## The queue

`money/queue.py` builds the ranked list:

* only `status == "ready"` clips with a render on disk,
* collapsed by `(project, start, end)` so re-renders do not appear twice,
* sorted by expected USD, then score,
* uploaded clips excluded by default (`include_posted=true` to see them).

State lives in `clips.meta.uploaded` (`{at, url, platform}`), so it survives
restarts and travels with the clip through exports. `POST /clips/{id}/uploaded`
is idempotent.

### Campaign pricing

Ad RPM answers "what is a view worth on this platform". A clipping campaign
answers a different question: "what does *this brief* pay per verified view",
usually a flat rate with no region factor. So the queue takes the rate from the
brief and reprices everything with it:

```bash
clipbait queue --rate 2.00                                  # $2.00 per 1K views
clipbait queue --campaign data/campaigns/clipmoney.md        # rate from the file
```

One line in the rules file is enough:

```
rate_per_1k: 2.00
```

With a rate set, `expected_usd = expected views / 1000 × rate`, the platform
with the most expected reach wins instead of the highest ad RPM, and the output
says which basis was used. Region and RPM assumptions are dropped, because the
campaign does not pay by viewer country — and the estimate says country tiers
and caps are not modelled, so the brief still has to be read.

### Export bundle

`POST /clips/{id}/export` writes an upload-ready folder under
`data/exports/<slug>/`:

```
clip.mp4          the vertical render, upload as-is
caption.txt       hook + description + hashtags, ready to paste
metadata.json     machine-readable copy, platform, estimate, posted state
check.json        pre-post check: pass/fail per campaign rule
README.md         what to post, where, the expected range, and the check
```

No absolute paths are returned: `dir` is relative to the data directory.

The caption body is the *spoken* line, never the detector's own note. Clips made
before that rule went in store the note as the description ("5.0/s chat velocity
(5.4σ over baseline)"), so `clipbait recaption` rewrites those from the clip's
quote -- leaving `reason` on the clip for explainability -- and is a dry run
until `--apply`:

```bash
clipbait recaption            # show the before/after for every ready clip
clipbait recaption --apply    # write the fixes
```

---

## Before you post: the check

A render that looks right can still be rejected by the campaign: 25 fps, no
audio track, 12 seconds instead of 15, a banned word in the hook, a missing
hashtag. `money/check.py` catches that before the upload.

```bash
clipbait campaign                      # write data/campaigns/campaign.md
clipbait check clip_abc123             # uses the clip's own caption
clipbait check ./clip.mp4 --campaign data/campaigns/clipmoney.md \
  --caption "hook

#gta6 #clipmoney"
```

Rules file (only uncommented lines count):

```
min_seconds: 15
max_seconds: 60
required_hashtags: #gta6 #clipmoney
required_mention: @clipmoney
banned_terms: spoiler, early build
rate_per_1k: 2.00
```

Always checked, no config: file exists, probes as video, 1080x1920, ~30 fps,
audio track present, duration within the rules, no piracy/mod-menu wording,
required hashtags and mention present, caption under 2200 characters.

```
PASS  file exists
PASS  resolution 1080x1920  [1080x1920]
PASS  fps about 30  [30.00]
PASS  has audio
PASS  duration 15-60s  [38.0s]
PASS  no banned terms in caption/hook
PASS  required hashtags  [#gta6 #clipmoney]
RESULT: PASS
```

Exit code is `1` on any FAIL, so a cron job or CI step can gate the upload. If
no rules file is found the report says so instead of pretending it verified a
campaign: defaults are a sanity check, not a compliance check.

---

## Finding a live room

The best moment in a 40-viewer stream earns nothing. `clipbait streams` searches
YouTube's own live filter and ranks by the concurrent viewer count YouTube
returns, so the room you enter already has distribution:

```bash
clipbait streams --query "gta 6" --limit 15
clipbait streams --query "gta 6" --json > rooms.json
clipbait streams --query "gta 6" --out rooms.md
clipbait streams --query "gta 6" --watch    # monitor the biggest room now
```

```
   1. 139,774 viewers  IShowSpeed   PLAYING GTA 6 RIGHT NOW ...  https://youtube.com/watch?v=...
   2.  83,575 viewers  TheBurntPeanut  LIVE | GTA 6 | DAY 2 ...  https://youtube.com/watch?v=...
   3.  54,936 viewers  Kai Cenat Live  PLAYING GTA 6 ...         https://youtube.com/watch?v=...
```

Numbers are what the platform reported at run time; nothing is extrapolated.
Search results are only candidates - the flat result omits `is_live` and viewer
counts - so each one is resolved against its watch page before ranking.

---

## CLI

```bash
# What should I post next?
clipbait queue

# Machine-readable, for a scheduler or a spreadsheet
clipbait queue --json --limit 50 --min-score 60

# Write bundles for every listed clip
clipbait queue --export

# Price against the campaign, not ad RPM
clipbait queue --campaign data/campaigns/clipmoney.md
clipbait queue --rate 2.00 --export

# Record what you posted (hides it from the default queue)
clipbait queue --mark clip_abc123 --url "https://tiktok.com/@me/video/1" \
  --platform tiktok

# Find the biggest live room, then watch it
clipbait streams --query "gta 6" --limit 15
clipbait streams --query "gta 6" --watch

# Gate the upload on the campaign's actual rules
clipbait campaign
clipbait check clip_abc123 --campaign data/campaigns/clipmoney.md

# Post it: dry run first, then for real, at the best window
clipbait publish                                  # plan everything, send nothing
clipbait publish --live --campaign data/campaigns/clipmoney.md
clipbait publish clip_abc123 --at 2026-11-21T19:30 --timezone America/New_York
clipbait publish clip_abc123 --live --now         # skip the window, post now
```

Output is one line per clip:

```
  1. $   3.42  ($0.41-$14.02)  tiktok   86   38s  Chat: "NO WAY"
```

The `$` range is the honest part: plan around the left number.

---

## API

```
GET  /api/v1/queue?limit=20&min_score=0&include_posted=false
POST /api/v1/clips/{id}/uploaded   {url?, platform?}
POST /api/v1/clips/{id}/export
```

`GET /queue` returns:

```json
{
  "generated_at": "...",
  "count": 3,
  "total_expected_usd": 7.14,
  "posted_count": 0,
  "items": [
    {
      "clip_id": "clip_...",
      "platform": "tiktok",
      "region": "US",
      "expected_usd": 3.42,
      "usd_low": 0.41,
      "usd_high": 14.02,
      "expected_views": 7600,
      "rpm": 0.45,
      "confidence": "medium",
      "copy": {"title": "...", "caption": "...", "hashtags": "#shorts #viral"},
      "payout": {"estimates": ["...all five platforms with assumptions..."]}
    }
  ]
}
```

`GET /api/v1/meta` exposes the RPM table under `money.platforms`, so the UI
never hardcodes a rate.

---

## UI

**Money queue** in the sidebar (`/queue`). Summary cards (expected total,
ceiling, dominant platform, top clip), then one row per clip: rank, platform,
region, duration, score, expected range, RPM, evidence chips, and the actions
that matter — Watch, Copy caption, Export, Mark uploaded.

The ceiling card is the sum of every clip's 80th percentile. It is there to show
the upside; the expected total is the number to use.

---

## The operator-in-the-loop pass

Detection can be wrong in both directions, and a campaign clip has to be an
actual moment, not a random 40 seconds of gameplay. The reliable route is the
one a human clipper already uses: watch, note the timestamp, cut exactly that.

```bash
clipbait grab "<vod-or-live-url>" --at 1:24:30 --seconds 40 \
  --language en --campaign data/campaigns/kai-gta6.md --export
```

`--at` accepts `75`, `1:15` and `0:01:15`; `--seconds` is the length of the
window. Only that window is fetched, so the cost is the clip, not the VOD. The
grab runs the same transcript → hook → caption → reframe → forecast path as a
detected moment, then writes the export bundle and prints the check result.

Use it after the stream ends on the VOD: timestamps there are exact, the
thumbnail strip tells you where the moment is, and nothing about the result
depends on the detector's opinion.

## The unattended pass

`scripts/earn.sh` chains the whole pipeline when nobody is watching the terminal:

```bash
./scripts/earn.sh                                  # discover -> watch 30m -> export
QUERY="kai cenat" MINUTES=45 MAX_CLIPS=6 ./scripts/earn.sh
CAMPAIGN=data/campaigns/clipmoney.md ./scripts/earn.sh
SOURCE="https://youtube.com/watch?v=..." ./scripts/earn.sh   # skip discovery
```

It finds the biggest live room for the query, monitors it with on-stream
detection, then writes an export bundle for every clip above `MIN_SCORE`. Pass
`PUBLISH=1` to also hand the bundles to `clipbait publish --live`.

`earn.sh` is a single pass. When you want the pass to repeat by itself, that is
`clipbait autopilot`:

```bash
clipbait autopilot --query "gta 6" --rounds 4 --minutes 30 --live \
  --campaign data/campaigns/clipmoney.md
clipbait autopilot --source "https://youtube.com/watch?v=..." --rounds 0   # until Ctrl-C
```

Each round it discovers the biggest live room (or watches `--source`),
monitors it for `--minutes` **or** until `--max-clips` clips have finished
rendering, whichever comes first (only ready clips count, and a round drains
renders still in flight), stops the monitor, packages that round's clips, and — only with
`--live` — posts them at their windows. Then it cools down and picks the next
room it has not already watched. Without `--live` it is a full dry loop:
discover, watch, clip and package, but send nothing.

The round decisions (`round_finished`, `more_rounds`, `pick_stream`) are pure and
tested against a fake clock. Every side effect the loop performs — discover,
start, status, stop, clips, export, publish, sleep — sits behind one replaceable
`Runtime`, so the loop is exercised end to end in tests with a fake clock and no
network, ffmpeg or real sleep.

## Publishing

The queue decides *what* to post. `publish` decides *when* and *where*, and
sends it. It is dry by default -- it prints the destination, the caption and the
exact moment for every clip and sends nothing until `--live` is passed.

```bash
clipbait publish                                  # dry run over the queue
clipbait publish --live --limit 5                 # send the top 5
clipbait publish clip_abc123 --live --now         # this one, immediately
clipbait publish clip_abc123 --at 2026-11-21T19:30 --timezone America/New_York
```

It publishes through [Upload-Post](https://upload-post.com), which posts to
TikTok, Instagram, YouTube, LinkedIn, X, Facebook and more from one request. Two
environment variables turn it on:

```bash
export UPLOAD_POST_API_KEY=...        # from the Upload-Post dashboard
export UPLOAD_POST_USER=your_profile  # profile holding your connected accounts
```

**Where the time comes from.** The clip is scheduled with `scheduled_date` +
`timezone`, so Upload-Post publishes it at that moment without Clipbait staying
up. The moment is chosen in this order:

1. `--at` -- an exact local time you name (`source: manual`).
2. `--now` -- post as soon as the provider accepts it (`source: now`).
3. the account's **measured** best hours from Upload-Post, when the account has
   enough history (`source: account-best-times`).
4. the region profile's curated posting windows, read in the market's own
   timezone, e.g. `18:00-22:00` `America/New_York` for `US`
   (`source: region-window`).

`--timezone` overrides which zone the windows are read in; `--offset` picks the
minute inside the window (default: a stable per-clip spread, so a batch does not
all fire at once).

**Copy is shaped per network.** Upload-Post reads `title` as the caption on the
short-form networks and `description` on YouTube/LinkedIn/Facebook. `publish`
sends both, plus the platform override fields, so one caption block is right
everywhere instead of truncated on whichever network is unlucky: a YouTube title
stays inside its limit, an X post stays a single tweet, and the campaign's
required hashtags/mention are merged in before the send.

**Idempotent, and honest.** Every send carries
`Idempotency-Key: clipbait-<clip_id>`; a clip is marked posted only after the
provider accepts it. A network refusal (rate limit, reconnect needed, a cap) is
reported with its `error_code` and leaves the clip unposted so the next run can
retry. Nothing is ever marked posted by a dry run.

## Posting is the last mile

Everything up to here is automated. What is not automated, and should not be
pretended otherwise:

1. Join the campaign server (Clip Money: <https://discord.gg/FnCMF4KMAT>).
2. Connect the TikTok/Reels/Shorts account that will post **to Upload-Post** (so
   `publish` can reach it), and let the platform report audience stats. The
   account gets a country tier, and some campaigns only accept certain tiers.
3. Opt into a campaign and **read its brief**, then paste the real rules into
   `data/campaigns/<campaign>.md` (see the check above). The brief is the only
   source of truth for duration, required hashtags, mention, sound and whether
   footage must come from the campaign's clip bank.
4. Post from that account -- `clipbait publish --live --campaign
   data/campaigns/<campaign>.md` sends the bundles at their windows -- then
   submit the link in the campaign's submit channel. Submission stays manual:
   it is the claim that ties a post to a campaign, and only you can make it.
   Approval is usually within an hour; views update every two hours.
5. Payment goes to PayPal when the campaign ends. Keep view/like counts public
   or tracking cannot verify them.

Rates move. On 2026-11-20 the Kai Cenat campaign listed **$2.00 per 1,000
verified views** with a $25K budget (62% left); Clip's best live rate that day
was $3.00. Treat any number here as a snapshot, not a promise.

---

## Choosing platforms in practice

* **Volume plays** (gaming, reactions, stream clips): TikTok and Reels. Expect
  low RPM and high variance. Post several clips, expect most to do nothing.
* **RPM plays** (podcasts, tech, business, anything with a professional angle):
  LinkedIn and X. Fewer views, higher value per view, and a 60–90s clip is fine.
* **Shorts** pays least per view but has the deepest reach for search-driven
  content.
* A clip can go to more than one platform. The queue recommends the best *first*
  destination; nothing stops you from exporting once and posting twice.

Do not chase the ceiling. The operator's job is to post consistently and read
the low end as the plan; the money layer only removes the guesswork about what
to post next and where.