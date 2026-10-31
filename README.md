# Star growth dashboard

Every public GitHub repository above 2,000 stars, forks included, with the stars it gained over the past 1, 7, 15, 30,
90, 180 and 365 days, as a static site on GitHub Pages: **https://neo-giga.github.io/star-growth-dashboard/**. No server,
no login, no tracking: the browser loads one columnar file and sorts, searches and filters it locally.
Agents start at the live catalog, [`api/v2/catalog.json`](https://neo-giga.github.io/star-growth-dashboard/api/v2/catalog.json),
or [`llms.txt`](llms.txt); the contract is [docs/agent-feed.md](docs/agent-feed.md).

## What is live

- **The page** (`index.html`, `app.js`, `style.css`): one table with a column per window, sortable by any column
  (repository, language, creation date, stars or any gain), name search, language filter, 50-row pages, and a card layout on
  narrow screens. Gains that came from GitHub's star history rather than two daily snapshots are marked ≈. Expanding a row
  fetches live details from the GitHub API (see *Live details and rate limits*).
- **The agent feed** (`api/v2`, schema `2.0`): a catalog pointing at one content-addressed snapshot with top-100 files per
  window (JSON and Markdown), the full set as JSONL and CSV, the page's columnar file, a schema and a manifest. These files
  are generated at publish time and exist only on the live site, not in git.
- **Daily star capture** records the star count of every repository above the threshold on the orphan `data` branch, every
  day, kept forever; **Publish the page** rebuilds and deploys the site from it after each capture.

Read the current date, row count and per-window sources from the live catalog rather than from this file. At the first v3
deploy (capture of 2026-10-31) it listed 34,219 repositories (99.997% of the search total), the 1-day column exact from
snapshots, and the other six columns from history with 290 nulls each (0.85%).

## Repository layout

| branch / path | what it holds |
|---|---|
| `main`: `index.html`, `app.js`, `style.css`, `llms.txt`, `docs/agent-feed.md` | the UI and agent-facing sources the site is built from |
| `main`: `scripts/daily_capture.py`, `capture/`, `scripts/build_windows.py` | the daily capture and the page-ready `windows/latest.json` |
| `main`: `scripts/publish_page.py`, `publish/`, `scripts/deploy_paths.py` | the publish gate, the site and `api/v2` staging, the stale-run check |
| `main`: `scripts/backfill_history.py` | the one-time history backfill (run on a Mac with a personal token; already done) |
| `main`: `.github/workflows/*.yml`, `docs/*.yml` | the two installed workflows; the `docs/` copies are kept byte-identical |
| `main`: `tests/` | the unit suite (no network: `tests/fake_github.py`) |
| `data` (orphan branch) | `repos.json`, `slices.json`, `meta.json`, `snapshots/YYYY/YYYY-MM-DD.json`, `history/XX.json`, `diagnostics/`; layout and semantics in [docs/data-layout.md](docs/data-layout.md) |

`main` still carries published files from the earlier upstream-feed site (`data*.json`, hashed `app.*.js` / `style.*.css`,
`api/v1/`). Nothing serves or reads them; [PR #9](https://github.com/neo-giga/star-growth-dashboard/pull/9)
deletes them. The `api/v1` feed is no longer published.

## The two workflows

| | Daily star capture | Publish the page |
|---|---|---|
| file | [`.github/workflows/daily-capture.yml`](.github/workflows/daily-capture.yml) | [`.github/workflows/publish-page.yml`](.github/workflows/publish-page.yml) |
| triggers | `30 5 * * *` (05:30 UTC, clear of Pacific midnight, the snapshot's date); manual dispatch | every completed *Daily star capture* run on `main`; push to `main` touching `index.html`, `app.js`, `style.css`, `llms.txt`, `docs/agent-feed.md`, `scripts/**`, `publish/**`, `capture/**` or the workflow itself; manual dispatch |
| writes | one `capture YYYY-MM-DD` commit per day on the `data` branch, as `github-actions[bot]` | the `github-pages` artifact, then Pages. **Nothing is committed to git** |
| token | built-in `GITHUB_TOKEN` only: `contents: write` on this one job | built-in `GITHUB_TOKEN` only: `contents: read`, `pages: write`, `id-token: write` |
| job limit | 110 min (`--deadline-minutes 75` for discovery) | 20 min |
| concurrency | `daily-star-capture` | `public-feed-publication` |
| diagnostic | `capture-diagnostic-<run_id>` artifact, 14 days; also `diagnostics/YYYY/YYYY-MM-DD.json` on `data` | `publish-diagnostic-<run_id>` artifact, 7 days |

Both run in GitHub's cloud with the repository's own token: no personal token, no secret and no Mac is needed for the daily
jobs. GitHub starts scheduled runs late when its queue is busy (on 2026-10-31 the capture started at 11:03 UTC); the
publication follows whenever the capture completes.

**Manual runs** (Actions tab, *Run workflow*):

- *Daily star capture*: `threshold` (default `2000`), `skip_discovery` (snapshot the known set only, no search for new
  repositories).
- *Publish the page*: no inputs; re-publishes the `data` branch's latest capture with the current `main` UI.

### The publish gate

`scripts/publish_page.py` (rules in `publish/stage.py`) refuses, and the current page stays, when the windows file is older
than 36 hours, its row count is under 99% of that day's search total, or any gain column is null for more than 1% of rows.
The one exception: when a missed capture leaves a window without its baseline snapshot on or after its `exact_since`, that
day's column is exempt from the null rule and the page notes it. The workflow never passes `--ignore-gate`; it is for local
previews only. A run also stops before deploying, red at *Refuse to deploy a stale run*, when live `main`'s UI or scripts no
longer match the run's: the run the newer sources triggered publishes instead.

## Data semantics

- **A gain over N days is the stars gained in the N × 24 hours ending at the capture.** Once a snapshot N days old exists it
  is the exact net change between two daily counts (`snapshot`, unstars included); until then it comes from GitHub's per-day
  star history (`history`, approximate: stars given and later removed are not counted). Each window's `exact_since` in the
  catalog is the day its column switches to snapshots; the 365-day column switches on 2027-10-29.
- **Unknown is null, never zero.** Unknown gains, languages and dates are `null` in JSON, empty cells in CSV and `—` on the
  page; they sort last. A repository missing from the set is not known to be deleted.
- **Dates are US Pacific days**: the 05:30 UTC capture is dated the Pacific day it ends.
- **Only the latest snapshot is served.** Snapshot URLs are content-addressed and immutable, but the previous snapshot's URLs
  stop resolving after the next deploy; keep the rows you depend on.

Details: [docs/agent-feed.md](docs/agent-feed.md) (the feed) and [docs/data-layout.md](docs/data-layout.md) (the branch,
history records, `windows/latest.json`, budget).

## Live details and rate limits

Expanding a row calls `api.github.com` from the browser, unauthenticated: the repository record plus three counts
(contributors, issue comments, PR review comments), four requests per expansion. GitHub allows 60 unauthenticated requests
per hour per IP, so about fifteen expansions. Definite answers are cached in the tab's `sessionStorage`; at the limit the
panel shows `GitHub limit reached — resets HH:MM` and the fields stay `—` until then. The ranking itself never calls GitHub.

## Local preview and testing

Python 3 and the standard library are enough; nothing to install. Keep scratch output under your temporary directory:

```sh
python3 -m unittest discover -s tests          # about 20 s, no network

# Stage the site exactly as the publish workflow does, from a checkout of the data branch
out="$(mktemp -d)"                                              # under $TMPDIR
git fetch origin data && git worktree add --detach "$out/data" FETCH_HEAD
python3 scripts/build_windows.py --data-dir "$out/data"        # writes windows/latest.json (gitignored there)
python3 scripts/publish_page.py --site-dir . --data-dir "$out/data" --output-dir "$out/site"   # --output-dir must not exist
python3 -m http.server -d "$out/site" 8000                      # then open http://localhost:8000/
git worktree remove "$out/data"                                 # when done
```

`publish_page.py` exits `2` and stages nothing when the gate refuses; add `--ignore-gate` to preview anyway.

The capture runs against the same `data` checkout and your `gh` login (`GH_TOKEN` or `GITHUB_TOKEN` are used when set):

```sh
python3 scripts/daily_capture.py --data-dir "$out/data" --dry-run --skip-discovery --sample 200 --max-history 0
```

The dry run writes into a temporary directory it prints and never touches the checkout.

## Troubleshooting

- **Publish run red at *Stage the page from windows/latest.json, or refuse***: nothing deployed, the page is unchanged.
  `publish-diagnostic-<run_id>` lists the `problems` and the `measurements` (age, coverage, null share per column). The next
  capture triggers another publication; a manual *Publish the page* run retries now.
- **Publish run red at *Refuse to deploy a stale run***: `main` changed during the run; the run that change triggered
  publishes instead.
- **Page *Updated* time more than a day old**: check the latest *Daily star capture* run and the *Publish the page* run it
  triggered.
- **UI change merged but the page looks old**: only the push paths listed above trigger a publication. Changes to
  `README.md`, `tests/` or other docs trigger nothing.
- **Capture run red** at *Fail the run when coverage is under the floor*: the day's files were still committed; the snapshot
  covered under 99% of the search total. `capture-diagnostic-<run_id>` has the slices listed, failed and skipped. If it also
  holds `unpushed-data-commit.patch`, the push to `data` lost a race three times or conflicted: apply the patch by hand.
- **Checking the data branch**: `git fetch origin data && git log --oneline FETCH_HEAD` shows one `capture YYYY-MM-DD`
  commit per day.

## Documents

- [docs/agent-feed.md](docs/agent-feed.md): the `api/v2` contract, record fields, gain sources, coverage and rights.
- [docs/data-layout.md](docs/data-layout.md): the `data` branch, history records, `windows/latest.json`, budget.
- [docs/daily-capture.yml](docs/daily-capture.yml), [docs/publish-page.yml](docs/publish-page.yml): copies of the installed workflows.
