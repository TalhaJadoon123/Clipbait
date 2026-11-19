# Contributing

Thanks for taking a look. Clipbait is deliberately small: a Python package, one
SPA and a test suite. You can be productive in a few minutes.

## Setup

```bash
git clone https://github.com/<you>/Clipbait.git
cd Clipbait
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
clipbait doctor
pytest
```

Optional, only if you touch the UI:

```bash
cd apps/web
npm install
npm run dev      # proxies /api to :8000
npm run build    # writes dist/ (committed)
```

## Ground rules

1. **Tests first.** The risky logic is pure and cheap to test: spike detection,
   segment selection, ranking, ASS generation, packaging, the SSRF guard. If a
   change touches one of those, add a failing test, then make it pass.
2. **Keep orchestration in `pipeline.py`.** API handlers validate and enqueue.
   CLI commands build options and call the same functions. If both front doors
   would need the same new step, it belongs in the pipeline.
3. **No new infrastructure.** No broker, no external database, no required API
   key. Optional extras are welcome; mandatory services are not.
4. **Stay data-driven.** New markets and meme formats are tuples, not prompts.
5. **Security defaults stay safe.** Any new outbound fetch goes through
   `net_guard`. Any new file path is resolved under the data directory. Media is
   served by id, never by path.
6. **Immutability at boundaries.** Return new dicts/models rather than mutating
   rows passed in. `Settings` and the virality profiles are frozen dataclasses.

## Style

* Python 3.11+, type hints on public functions, `from __future__ import annotations`.
* Ruff profile is in `pyproject.toml` (line length 100, `E/F/I/UP/B`).
  `make lint` and `make fmt`.
* Comments explain **why**, especially where a naive implementation would be
  wrong (median vs mean, clock rebasing, encoder validation, sendcmd dedupe).
* Frontend: React + TypeScript, plain CSS in `apps/web/src/styles.css`. No UI
  framework, no CSS-in-JS. Keep components small and typed against `types.ts`.

## What to include in a PR

* What changed and why, in one short paragraph.
* How you verified it (commands, and the output that proves it).
* For UI changes, a screenshot or a short clip.
* For a new market/meme, the brief and the `avoid` list, not just the copy.

## Commit style

Conventional commits, imperative mood:

```
feat: add Thai market profile
fix: rebase live chat timestamps onto the recorder clock
docs: explain the forecast weights
test: pin cooldown behaviour after a second burst
```

## Licensing

By contributing you agree your work is licensed under the MIT license in
[`LICENSE`](LICENSE). Do not copy code from non-commercial projects; techniques
can be reimplemented, code cannot be relicensed. If you adapt an idea, add the
credit to [`NOTICE`](NOTICE).