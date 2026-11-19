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
README.md         what to post, where, and the expected range
```

No absolute paths are returned: `dir` is relative to the data directory.

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