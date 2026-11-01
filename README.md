# Star growth dashboard

**A daily, machine-readable GitHub repository discovery dataset for agents to find accelerating projects across the full
tracked set.** Its primary users are agents in any harness (Claude Code, Muse, Grok bots, or anything that can make an HTTPS
request): every public repository over the star minimum (the catalog's `min_stars`), forks included, with its star count and
the stars it gained over the past 1, 7, 15, 30, 90, 180 and 365 days, as static files any agent can fetch and filter itself.

- **Start here:** [`api/v2/catalog.json`](https://neo-giga.github.io/star-growth-dashboard/api/v2/catalog.json), the
  current pointer: capture date and time, row count, `min_stars`, the seven windows with their sources, the capture's
  completeness (`capture`), the pending-history count (`history`), and links to the files below.
- **Portable files over HTTPS:** the full set as JSONL and CSV, the columnar JSON the page itself uses, top-100 JSON and
  Markdown per window, a JSON Schema for records, and a manifest with every file's size and SHA-256. All are static and
  content-addressed per capture. No key, no login, no query API.
- **Provenance in every answer:** each gain says whether it is exact (`snapshot`) or approximate (`history`); recently
  added repositories carry `history_pending`; the catalog says whether the day's capture was complete and why not.
- **Contract:** [docs/agent-feed.md](docs/agent-feed.md), plus [`llms.txt`](llms.txt) as a short index.
- **Top repositories for X:** for each GitHub topic carried by at least three repositories, a file with every matching
  repository ranked by 7-day gain, plus each repository's description and topics in `metadata.jsonl`. An agent maps X to
  topic slugs itself; the recipe is in [docs/agent-feed.md](docs/agent-feed.md). Topics are read gradually by the daily
  capture: the catalog's `metadata` block says how many repositories are covered (`absent` until the first read), and a
  repository whose topics are unknown cannot appear. Membership is the exact GitHub topic, not semantic search.

The webpage, **https://neo-giga.github.io/star-growth-dashboard/** (titled Momentum), is a secondary human explorer over the
same rows: one table sortable by any window, name search, language filter.

## Why it exists and when to use it

- **One fixed, uniform cohort.** Every repository that meets the star bar is in the set, and the same rows appear in all
  seven windows, so a ranking is a ranking of the whole cohort rather than of the repositories someone thought to look up.
- **Daily retained snapshots.** A scheduled capture records every repository's star count once a day on the `data`
  branch and keeps it; gains become exact differences of two of those counts as the snapshots accumulate.
- **A machine-readable bulk feed.** The full set as JSONL, CSV and a columnar file, plus top-100 files per window, for
  scripts and agents.
- **Explicit provenance.** Each capture says how complete it was (discovery, unread counts, the search total), each gain
  says whether it came from snapshots (exact) or from GitHub's star history (approximate), and repositories whose history
  is not read yet are marked pending instead of hidden or zeroed.

Use it when an agent (or a person) needs to scout what is gaining momentum in a period, scan a language for fast risers,
or pull a dated cohort for an analysis. Use a per-repository tool when you already know which projects you care about.

## How this differs from existing solutions

[Star History](https://www.star-history.com/) is the best-known example and does a lot already: per-repository star
history charts with several repositories in one chart, an "Align timeline" option and live README embeds
([how to use it](https://www.star-history.com/blog/how-to-use-github-star-history)), a weekly and all-time ranking on its
home page, a curated [Star Map](https://www.star-history.com/starmap) and [Star Compare](https://www.star-history.com/compare)
(side-by-side comparisons across 42 categories). Both projects read GitHub's star history endpoint
(`/repos/{owner}/{repo}/stargazers/history`; Star History describes its move to it in
[its 2026-09-05 post](https://www.star-history.com/blog/new-github-star-history-api)), so neither has exclusive or more
accurate data. The difference is focus: this repository is systematic monitoring of one fixed universe, with daily
snapshots saved for reproducibility, seven sortable windows over every row and a bulk feed. Star History is better for an
individual repository's curve, visual comparison and embeds; use the two together (open a repository you found here in
Star History to see its curve). This project does not draw charts and does not plan to.

For "which repositories in topic X are gaining stars fastest", the other sources an agent can reach today rank something
else. [OSS Insight](https://ossinsight.io/) has an agent API with curated collections and per-repository analytics, but
its star-based rankings and trending are derived from GitHub's public events firehose and have been unavailable since
2026-03-01: its API's own data-quality notice says event capture fell to roughly 0.3% of baseline, so the ordering would be
noise ([`/api/mcp?action=trending`](https://ossinsight.io/api/mcp?action=trending), `data_quality.status: unavailable`, read
2026-10-31; [its data-quality page](https://ossinsight.io/docs/data-quality)). Use it for curated domains and repository
analytics. GitHub's own topic search (`topic:X sort:stars`) ranks by total stars, which favours age and past hype over
current growth, and is rate-limited with no history. This dataset counts every repository's stars directly once a day, so
the event-firehose collapse does not affect it, and ranks growth inside any GitHub topic over the whole cohort, from a dated
snapshot, as static files. Star History stays complementary for the curve of any repository found here.

## Limits

- **Not every GitHub repository**: public repositories at or over the star bar, as far as GitHub search lists them at
  capture time. The search index moves during a capture, so a row count close to the search total is reported as such,
  not as a census.
- **Not real time**: one capture a day (05:30 UTC, often started late by GitHub's queue); the page shows its capture time.
- **A partial day is not published**: when GitHub search is degraded and discovery cannot list every slice, when there is no
  search total that day, or when a count read fails, the capture is still saved on the `data` branch but its run goes red
  and the previous day's files stay live. On such days the page and feed do not update; the catalog's `date` says which
  capture is served.
- **Approximate until exact baselines exist**: a window's gains come from GitHub's star history (marked ≈; stars given and
  later removed are not counted) until a daily snapshot N days old exists for that repository.
- **Pending history**: repositories that joined recently show their current stars and metadata at once; gains no snapshot
  covers yet are `null` (— on the page, with a line above the table counting them) until their history is read (a few hundred
  repositories a day, oldest first).
- Unknown languages, dates and gains are allowed and shown as unknown (`null`), never as zero.

## What is live, and what is new

The live page was built for the set above 2,000 stars (34,231 rows at the 2026-10-31 capture, every gain measured). The
`>= 1,000` set (66,915 repositories in GitHub search on 2026-10-31) takes over from the first complete capture published
after this change ships (more than one day if search is degraded: see *Limits*); its ~32,700 new rows start with pending history, read gradually by the daily capture. The live catalog's
`min_stars`, `capture` and `history` fields say which set is served and how far its history has come.

- **The page** (`index.html`, `app.js`, `style.css`), titled Momentum: one table with a column per window, sortable by
  creation date, stars or any gain, name search, language filter, 50-row pages, and a card layout on
  narrow screens. Gains that came from GitHub's star history rather than two daily snapshots are marked ≈. Under each name, the
  repository's captured description on one line (from a fingerprinted `descriptions.<hash>.json` the page loads after its first
  rows, built from the `data` branch's metadata shards; absent until metadata is captured); expanding a row shows it in full and
  fetches live details from the GitHub API (see *Live details and rate limits*).
- **The agent feed** (`api/v2`, schema `2.0`): a catalog pointing at one content-addressed snapshot with top-100 files per
  window (JSON and Markdown), the full set as JSONL and CSV, the page's columnar file, a schema and a manifest; once the
  daily capture has read repository metadata, also `metadata.jsonl`, the topic index `topics.json` and one file per topic. These files
  are generated at publish time and exist only on the live site, not in git.
- **Daily star capture** records the star count of every repository over the threshold (`--threshold 999`: at least 1,000
  stars) on the orphan `data` branch, every day, kept forever, and reads at most 400 missing histories a day; **Publish the page** rebuilds and deploys the site from it after each capture.

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

The published files left over from the earlier upstream-feed site (`data*.json`, hashed `app.*.js` / `style.*.css`,
`api/v1/`) were removed from `main` by [PR #9](https://github.com/neo-giga/star-growth-dashboard/pull/9). The `api/v1`
feed is no longer published.

## The two workflows

| | Daily star capture | Publish the page |
|---|---|---|
| file | [`.github/workflows/daily-capture.yml`](.github/workflows/daily-capture.yml) | [`.github/workflows/publish-page.yml`](.github/workflows/publish-page.yml) |
| triggers | `30 5 * * *` (05:30 UTC, clear of Pacific midnight, the snapshot's date); manual dispatch | every completed *Daily star capture* run on `main`; push to `main` touching `index.html`, `app.js`, `style.css`, `llms.txt`, `docs/agent-feed.md`, `scripts/**`, `publish/**`, `capture/**` or the workflow itself; manual dispatch |
| writes | one `capture YYYY-MM-DD` commit per day on the `data` branch, as `github-actions[bot]` | the `github-pages` artifact, then Pages. **Nothing is committed to git** |
| token | built-in `GITHUB_TOKEN` only: `contents: write` on this one job | built-in `GITHUB_TOKEN` only: `contents: read`, `pages: write`, `id-token: write` |
| job limit | 150 min (`--deadline-minutes 75` for discovery; no history batch starts after 110 min) | 20 min |
| concurrency | `daily-star-capture` | `public-feed-publication` |
| diagnostic | `capture-diagnostic-<run_id>` artifact, 14 days; also `diagnostics/YYYY/YYYY-MM-DD.json` on `data` | `publish-diagnostic-<run_id>` artifact, 7 days |

Both run in GitHub's cloud with the repository's own token: no personal token, no secret and no Mac is needed for the daily
jobs. GitHub starts scheduled runs late when its queue is busy (on 2026-10-31 the capture started at 11:03 UTC); the
publication follows whenever the capture completes.

**Manual runs** (Actions tab, *Run workflow*):

- *Daily star capture*: `threshold` (default `999`, i.e. at least 1,000 stars; `2000` still means more than 2,000), `skip_discovery` (snapshot the known set only, no search for new
  repositories).
- *Publish the page*: no inputs; re-publishes the `data` branch's latest capture with the current `main` UI.

### The publish gate

`scripts/publish_page.py` (rules in `publish/stage.py`) refuses, and the current page stays, when the capture was partial
(discovery did not list every slice, no search total that day, or a count read stayed unread), when the windows file and the
day's diagnostic come from different capture runs (an interrupted rerun), when the file is older than 36 hours, its row
count is under 99% of that day's search total, or any gain column is null for more than 1% of the rows whose history is not
pending. Pending means the repository entered the set without history and is queued in `history-pending.json`; a known
repository whose history record went missing or is incomplete is in the repair queue, never pending, so its nulls still count.
The one exception: when a missed capture leaves a window without its baseline snapshot on or after its `exact_since`, that
day's column is exempt from the null rule and the page notes it. The workflow never passes `--ignore-gate`; it is for local
previews only. A run also stops before deploying, red at *Refuse to deploy a stale run*, when live `main`'s UI or scripts no
longer match the run's: the run the newer sources triggered publishes instead.

## Data semantics

- **A gain over N days is the stars gained in the N × 24 hours ending at the capture.** Once a snapshot N days old exists it
  is the exact net change between two daily counts (`snapshot`, unstars included); until then it comes from GitHub's per-day
  star history (`history`, approximate: stars given and later removed are not counted). Each window's `exact_since` in the
  catalog is the day its column switches to snapshots; the 365-day column switches on 2027-10-29.
- **Pending history is shown, not hidden.** A repository that joined without history keeps its row in every window with
  `history_pending` true (— on the page, with a hover text saying the gain arrives once its history is read); its gains that no
  snapshot covers are null until its history is read.
- **Capture completeness** is recorded per capture (`capture` in the catalog and the page file): `complete` only when every
  discovery slice was listed, the search total is that day's, no count batch stayed unread and coverage is at least 99%;
  otherwise `partial` with the reasons. A same-day rerun keeps the earlier run's counts for repositories it cannot read.
- **Unknown is null, never zero.** Unknown gains, languages and dates are `null` in JSON, empty cells in CSV and `—` on the
  page; they sort last. A repository missing from the set is not known to be deleted.
- **Dates are US Pacific days**: the 05:30 UTC capture is dated the Pacific day it ends.
- **Only the latest snapshot is served.** Snapshot URLs are content-addressed and immutable, but the previous snapshot's URLs
  stop resolving after the next deploy; keep the rows you depend on.

Details: [docs/agent-feed.md](docs/agent-feed.md) (the feed) and [docs/data-layout.md](docs/data-layout.md) (the branch,
history records, `windows/latest.json`, budget).

## Live details and rate limits

Expanding a row calls `api.github.com` from the browser, unauthenticated: the repository record
plus three counts (contributors, issue comments, PR review comments), four requests per expansion.
GitHub allows 60 unauthenticated requests per hour per IP, so about fifteen expansions. Definite
answers are cached in the tab's `sessionStorage`; at the limit the panel shows
`GitHub limit reached — resets HH:MM` and the fields stay `—` until then. The ranking itself never
calls GitHub.

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
```

`publish_page.py` exits `2` and stages nothing when the gate refuses; add `--ignore-gate` to preview anyway.

The capture runs against the same `data` checkout and your `gh` login (`GH_TOKEN` or `GITHUB_TOKEN` are used when set):

```sh
python3 scripts/daily_capture.py --data-dir "$out/data" --dry-run --skip-discovery --sample 200 --max-history 0
```

The dry run writes into a temporary directory it prints and never touches the checkout.

When done with both, remove the `data` checkout: `git worktree remove "$out/data"`.

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
- **Capture run red** at *Fail the run when coverage is under the floor*: the day's files were still committed, but the
  capture was partial: `completeness.reasons` in the diagnostic says why (discovery partial or failed, typically degraded
  search; no search total; a count read unread after its retry, `counts.known_unread` / `entrants_unread`; coverage under
  99%). Its publication is refused and yesterday's page stays. A rerun the same Pacific day reads them again and keeps the counts it already has. `capture-diagnostic-<run_id>` has the slices listed, failed and skipped. If it also
  holds `unpushed-data-commit.patch`, the push to `data` lost a race three times or conflicted: apply the patch by hand.
- **Checking the data branch**: `git fetch origin data && git log --oneline FETCH_HEAD` shows one `capture YYYY-MM-DD`
  commit per day.

## Documents

- [docs/agent-feed.md](docs/agent-feed.md): the `api/v2` contract, record fields, gain sources, coverage and rights.
- [docs/data-layout.md](docs/data-layout.md): the `data` branch, history records, `windows/latest.json`, budget.
- [docs/daily-capture.yml](docs/daily-capture.yml), [docs/publish-page.yml](docs/publish-page.yml): copies of the installed workflows.
