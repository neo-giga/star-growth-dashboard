# Star growth dashboard

GitHub repositories ranked by stars gained over the past day, week or month, as a static site on
GitHub Pages: **https://neo-giga.github.io/star-growth-dashboard/**. No server, no login, no
tracking: a browser loads one JSON file per window and sorts, searches and filters it locally.
Agents start at [`api/v1/catalog.json`](api/v1/catalog.json) and [`llms.txt`](llms.txt); the
contract is [docs/agent-feed.md](docs/agent-feed.md).

## What is live today (2026-10-31)

- **The page** (`index.html`, `app.js`, `style.css`) with a day / week / month selector, sort by
  gain, total stars or creation date, name search, language filter and 100-row pages. Expanding a
  row fetches live details from the GitHub API (see *Live details and rate limits*).
- **Daily public feed refresh** rebuilds the site from an upstream trending feed and deploys it as a
  Pages artifact. The upstream feed is a third-party ranking, not GitHub itself; its limits
  (window endpoints unknown, not all of GitHub, month list read to a bounded depth) are spelled
  out in [docs/agent-feed.md](docs/agent-feed.md).
- **Daily star capture** (merged as [#6](https://github.com/neo-giga/star-growth-dashboard/pull/6))
  records the star count of every public repository above 2,000 stars on the orphan `data` branch,
  every day, kept forever. **Nothing on the page reads it yet**; that is
  [PR #7](https://github.com/neo-giga/star-growth-dashboard/pull/7), open and deliberately held
  (see *Pending: PR #7*).

## Repository layout

| branch / path | what it holds |
|---|---|
| `main`: `index.html`, `app.js`, `style.css`, `llms.txt`, `docs/agent-feed.md` | the UI and agent-facing sources the site is built from |
| `main`: `scripts/weekly_refresh.py`, `trending/` | the feed refresh: read the upstream feed, verify, stage the site, write the `api/v1` snapshot |
| `main`: `scripts/daily_capture.py`, `capture/`, `scripts/build_windows.py`, `scripts/backfill_history.py` | the star capture, the page-ready windows file and the one-time history backfill |
| `main`: `.github/workflows/*.yml`, `docs/*.yml` | the two installed workflows; the `docs/` copies are kept byte-identical |
| `main`: `tests/` | the unit suite (219 tests, no network: `tests/fake_github.py`) |
| `main`: `data.json`, `data.<hash>.json`, `app.<hash>.js`, `style.<hash>.css`, `api/v1/snapshots/` | leftovers of earlier commit-based deploys. The site is no longer served from git; the refresh reads them only to seed cached creation dates. PR #7's follow-up removes them |
| `data` (orphan branch) | `repos.json`, `slices.json`, `meta.json`, `snapshots/YYYY/YYYY-MM-DD.json`, `history/XX.json`, `diagnostics/`; layout and semantics in [docs/data-layout.md](docs/data-layout.md) |

## The two workflows

They are independent: different triggers, concurrency groups, permissions and outputs. A red run of
one says nothing about the other.

| | Daily public feed refresh | Daily star capture |
|---|---|---|
| file | [`.github/workflows/weekly-refresh.yml`](.github/workflows/weekly-refresh.yml) (name kept for its run history) | [`.github/workflows/daily-capture.yml`](.github/workflows/daily-capture.yml) |
| schedule | `23 7 * * *` (07:23 UTC) | `30 5 * * *` (05:30 UTC, before the refresh and clear of Pacific midnight) |
| other triggers | push to `main` touching `index.html`, `app.js`, `style.css`, `llms.txt`, `docs/agent-feed.md`, `scripts/**`, `trending/**` or the workflow itself (runs `validate-current`); manual dispatch | manual dispatch only |
| writes | the `github-pages` artifact, then Pages. **Nothing is committed to git** | one commit per day on the `data` branch, as `github-actions[bot]` |
| token | `GITHUB_TOKEN` only: `contents: read`, `actions: write`, `pages: write`, `id-token: write` | `GITHUB_TOKEN` only: `contents: write` on this one job |
| job limit | 360 min (`--deadline-minutes 330`) | 110 min (`--deadline-minutes 75` for discovery) |
| diagnostic | `refresh-diagnostic-<run_id>` artifact, 7 days | `capture-diagnostic-<run_id>` artifact, 14 days |

GitHub starts scheduled runs late when its queue is busy: on 2026-10-31 the capture started at
11:03 UTC and the refresh at 13:43 UTC.

**Manual runs** (Actions tab, *Run workflow*):

- *Daily public feed refresh*: `mode` = `validate-current` (default: re-publish the last published
  data with the current UI, no feed read) or `refresh` (read the upstream feed). `day_depth`,
  `week_depth`, `month_depth` (`all` or a multiple of 100; defaults `all`, `all`, `20000`),
  `verify_top` (rows read twice, default `2000`), `max_pages` (`1000`), `top` (legacy alias),
  `bootstrap_from_live` (one-time: read the previous publication from the live site when no run
  has a readable artifact). Re-runs of a finished run are refused: dispatch a new one.
- *Daily star capture*: `threshold` (default `2000`), `skip_discovery` (snapshot the known set
  only, no search for new repositories).

### A refresh run can deploy and still be red

`scripts/weekly_refresh.py` stages each window independently and exits:

- `0`: every window refreshed; the run deploys and is green.
- `3`: at least one window failed its checks and **kept its previously published rows** (its own
  older *Updated* time, `coverage.refreshed: false` and `not_refreshed_reason` in the snapshot
  manifest). The run **deploys the other windows fresh**, then fails on its last step, *Fail the
  run when a window kept its previous rows*, so the owner is emailed. The carried windows are in
  `carried_windows` of the diagnostic.
- anything else: nothing is staged, the current site stays, the run is red at *Stage validated
  site*.

A run also stops before deploying, red at *Refuse to deploy a stale run*, when `main` received a
change to the deploy paths while it ran; in `refresh` mode it dispatches a replacement refresh.

The latest scheduled run, [41206870751](https://github.com/neo-giga/star-growth-dashboard/actions/runs/41206870751)
on 2026-10-31, is the exit-3 case. It deployed, then went red:

| window | rows | *Updated* shown on the page | state |
|---|---|---|---|
| day | 14,450 | 2026-10-31 14:11 UTC | fresh |
| week | 33,670 | 2026-10-31 14:45 UTC | fresh |
| month | 2,000 | 2026-10-28 11:48 UTC | carried: `month refresh not publishable: pass A head HTTPError: HTTP Error 503: Service Unavailable` |

The same day's *Daily star capture*, run [41195280781](https://github.com/neo-giga/star-growth-dashboard/actions/runs/41195280781),
succeeded and committed `capture 2026-10-31` to `data` (34,219 repositories above 2,000 stars). It
is a different workflow with a different output; the red refresh does not make it stale, and vice versa.

## Data semantics

- **Gain on the page and in `api/v1`** is the upstream feed's reported gain for its own window,
  carried through as `star_growth.value`. It is not reconstructed from daily snapshots, exact window
  endpoints and `source_as_of` are null, and each window is a different observed universe. Rows
  are ordered by that gain, descending, null last.
- **Unknown is null, never zero.** Unknown gains, counts, dates, languages and topics are `null` in
  JSON, empty cells in CSV and `—` on the page; they sort last. A repository missing from a window
  is not known to have gained nothing, and missing from the feed does not mean deleted from GitHub.
- **Each window has its own *Updated* time** (`fetched_at_end` of the pass that published it). A
  carried window keeps the older time while the others move on.
- **Only the latest snapshot is served.** Snapshot URLs are content-addressed, but the previous
  snapshot's URLs stop resolving after the next deploy; keep the rows you depend on.
- **On the `data` branch**, a gain over N days is the stars gained in the N × 24 hours before the
  capture: from two daily snapshots once they exist (exact net change, unstars included), else from
  GitHub's per-day star history (unstars removed retroactively). The rules, `windows/latest.json`
  and the budget are in [docs/data-layout.md](docs/data-layout.md).

## Live details and rate limits

Expanding a row calls `api.github.com` from the browser, unauthenticated: the repository record
plus three counts (contributors, issue comments, PR review comments), four requests per expansion.
GitHub allows 60 unauthenticated requests per hour per IP, so about fifteen expansions. Definite
answers are cached in the tab's `sessionStorage`; at the limit the panel shows
`GitHub limit reached — resets HH:MM` and the fields stay `—` until then. The ranking itself never
calls GitHub.

## Local preview and testing

Python 3 and the standard library are enough; nothing to install.

```sh
python3 -m unittest discover -s tests          # 219 tests, about 30 s, no network

# Stage the site locally from the current publication (same code path as the workflow's validate-current)
python3 scripts/weekly_refresh.py --site-dir . --output-dir /tmp/site --mode validate-current \
  --published-site https://neo-giga.github.io/star-growth-dashboard/
python3 -m http.server -d /tmp/site 8000      # then open http://localhost:8000/
```

`--mode refresh` reads the upstream feed; even a small `--top 200` run takes over fifteen
minutes because the second read of each window waits 910 s past the provider's cache window.

The capture code runs against a checkout of the `data` branch and your `gh` login
(`GH_TOKEN` or `GITHUB_TOKEN` are used when set):

```sh
git fetch origin data && git worktree add --detach ../star-growth-dashboard-data FETCH_HEAD
python3 scripts/build_windows.py --data-dir ../star-growth-dashboard-data     # writes windows/latest.json (gitignored there)
python3 scripts/daily_capture.py --data-dir ../star-growth-dashboard-data --dry-run --skip-discovery --sample 200 --max-history 0
```

The dry run writes into a temporary directory it prints and never touches the checkout.

## Troubleshooting

- **Refresh run red, last step only** (*Fail the run when a window kept its previous rows*): the
  deploy happened. Open `refresh-diagnostic-<run_id>` for `carried_windows` and each window's
  `not_refreshed_reason`; the page shows the carried window's older *Updated* time. Tomorrow's run
  retries; a manual `refresh` run retries now.
- **Refresh run red at *Stage validated site***: nothing deployed, the site is unchanged. The
  diagnostic names the failed check (pagination, two-pass agreement, tail coverage, a window that
  shrank below half its previous size, the time budget, or no readable previous artifact).
- **Refresh run red at *Refuse to deploy a stale run***: sources on `main` changed during the run;
  look for the replacement run it dispatched.
- **Every window older than a day**: no run has deployed; check the workflow's run list and the
  Pages environment. **One window older**: it was carried, see above.
- **UI change merged but the page looks old**: only the deploy paths listed above trigger the
  `validate-current` push run. Changes to `README.md`, `tests/` or other docs trigger nothing.
- **Capture run red** at *Fail the run when coverage is under the floor*: the day's files were
  still committed; the snapshot covered under 99% of the search total. `capture-diagnostic-<run_id>`
  has the slices listed, failed and skipped. If it also holds `unpushed-data-commit.patch`, the push
  to `data` lost a race three times or conflicted: apply the patch by hand.
- **Checking the data branch**: `git fetch origin data && git log --oneline FETCH_HEAD` shows one
  `capture YYYY-MM-DD` commit per day.

## Pending: PR #7

[PR #7](https://github.com/neo-giga/star-growth-dashboard/pull/7) (`v3-page`, PR B of v3) replaces
the upstream feed with the `data` branch: one table of every repository above 2,000 stars with
gains over 1, 7, 15, 30, 90, 180 and 365 days, a new publish workflow that runs after each capture,
an `api/v2` feed, and removal of the feed-refresh code. It is reviewed and mergeable but **held on
purpose**: its publish gate refuses a file with a gain column more than 1% null, and until the
one-year history backfill is compacted onto `data` every window from 7 days up is about 97% null
(33,020 of 34,219 rows on 2026-10-31). Merging now would only stop today's daily refresh. Paths it
adds (`publish-page.yml`, `scripts/publish_page.py`, `api/v2`) exist on that branch only.

## Documents

- [docs/agent-feed.md](docs/agent-feed.md): the `api/v1` contract, coverage, provenance and the refresh rules.
- [docs/data-layout.md](docs/data-layout.md): the `data` branch, history records, `windows/latest.json`, budget.
- [docs/weekly-refresh.yml](docs/weekly-refresh.yml), [docs/daily-capture.yml](docs/daily-capture.yml): copies of the installed workflows.
