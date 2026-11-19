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

# Record what you posted (hides it from the default queue)
clipbait queue --mark clip_abc123 --url "https://tiktok.com/@me/video/1" \
  --platform tiktok

# Find the biggest live room, then watch it
clipbait streams --query "gta 6" --limit 15
clipbait streams --query "gta 6" --watch

# Gate the upload on the campaign's actual rules
clipbait campaign
clipbait check clip_abc123 --campaign data/campaigns/clipmoney.md
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

## The unattended pass

`scripts/earn.sh` chains the whole pipeline when nobody is watching the terminal:

```bash
./scripts/earn.sh                                  # discover -> watch 30m -> export
QUERY="kai cenat" MINUTES=45 MAX_CLIPS=6 ./scripts/earn.sh
CAMPAIGN=data/campaigns/clipmoney.md ./scripts/earn.sh
SOURCE="https://youtube.com/watch?v=..." ./scripts/earn.sh   # skip discovery
```

It finds the biggest live room for the query, monitors it with on-stream
detection, then writes an export bundle for every clip above `MIN_SCORE`. It
does **not** upload: the last mile is a human with a verified account.

## Posting is the last mile

Everything up to here is automated. What is not automated, and should not be
pretended otherwise:

1. Join the campaign server (Clip Money: <https://discord.gg/FnCMF4KMAT>).
2. Connect the TikTok/Reels/Shorts account that will post, and let the platform
   report audience stats. The account gets a country tier, and some campaigns
   only accept certain tiers.
3. Opt into a campaign and **read its brief**, then paste the real rules into
   `data/campaigns/<campaign>.md` (see the check above). The brief is the only
   source of truth for duration, required hashtags, mention, sound and whether
   footage must come from the campaign's clip bank.
4. Post from that account, then submit the link in the campaign's submit
   channel. Approval is usually within an hour; views update every two hours.
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