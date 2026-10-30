# The `data` branch: daily star counts of every public repository above 2,000 stars

An orphan branch, written once a day by `.github/workflows/daily-capture.yml` (`scripts/daily_capture.py`) and once by the
one-time backfill (`scripts/backfill_history.py`, run on a Mac with a personal token). Nothing on `main` reads it yet; the
page will, through `windows/latest.json`, which is the interface described last and in most detail. Forks are in the set:
every search query says `fork:true` (34,193 repositories above 2,000 stars on 2026-10-29, 302 of them forks).

All dates are **US Pacific calendar dates**: GitHub's star history is bucketed by Pacific day (measured 2026-10-29), and the
daily capture runs at 05:30 UTC, i.e. 22:30 PDT / 21:30 PST, so a snapshot is dated the Pacific day it ends.

## Files

| path | written by | content |
|---|---|---|
| `repos.json` | daily | the known set: `{threshold, updated_at, repos: {node_id: {full_name, language, created_at, first_seen}}}`. No per-day field: today's count is the snapshot's, and a repo was last seen on the newest snapshot that lists it. A repo stays known after it drops below the threshold or is deleted; the page filters by today's count. |
| `slices.json` | daily | the star-range plan for the next discovery: `[{lo, hi}]`, the last `hi` null (open-ended). Each slice held at most 800 results the day before, so a healthy run lists without probing. |
| `meta.json` | daily | `first_snapshot_date` (the ring's start), `last_total_count` (+ `_at`, `_source`: `search` or `previous`), `last_capture_date`. |
| `snapshots/YYYY/YYYY-MM-DD.json` | daily, forever | `{schema, date, captured_at, threshold, rows, node_id: [...], stars: [...]}`, sorted by node ID, plain compact JSON (not gzipped: git delta-compresses consecutive days). One count per known repo that answered that day. |
| `history/XX.json` | backfill, daily | per-day star history by node ID, 256 shards by the first two hex digits of SHA-1(node ID): `{node_id: {start, days, through, anchor_at, anchor_stars, pages, has_more}}` (below). |
| `windows/latest.json` | daily, **not committed** | the page-ready file (below). Written by the capture into its working copy and listed in the branch's `.gitignore` (`/windows/`); the publisher rebuilds it from a checkout of the branch with `python3 scripts/build_windows.py --data-dir <checkout>` (same bytes as the capture's). There is no daily copy: a past day's page is that day's snapshot plus the history shards. |
| `diagnostics/YYYY/YYYY-MM-DD.json` | daily | the run's diagnostic: snapshot read/missing/points, discovery status and slices, entrants, coverage, the per-window sources, exit code. |

Every write is atomic (temporary file + rename), so a killed run leaves whole files only.

### Growth of the branch (measured 2026-10-29)

Simulated on the real set (the 33,890 repos of 2026-10-29 with their names, languages, creation dates and counts, plus 600
just under the threshold; ~28,900 counts change a day, median 1.7 stars a day): the backfill, then 14 days of the capture
code, each day committed and `git gc --aggressive`. **+84 KiB of pack a day (82–85 on every day but one `gc` swing that
reversed the next day), ~30 MiB a year**, plus 9.4 MiB once for the backfilled history. Pack per path after the 14 days:
`history/` 8.6 MiB (the backfill), `snapshots/` 1.3 MiB, `repos.json` 1.1 MiB, `diagnostics/` 11 KiB. Two choices keep it
there: `repos.json` carries no per-day field (a daily `stars` + `last_seen` cost 4.6 MiB of pack in 14 days), and
`windows/latest.json` is not committed (its gains change every day in every row: ~395 KiB of pack a day in any layout tried,
columnar or one row per line, ~141 MiB a year).

## History records

`GET /repos/{owner}/{name}/stargazers/history` returns 30 calendar weeks per page, newest first, each with 7 per-day counts
(Sunday first) of the stars **still present** that were given on that day; later unstars are removed retroactively. Two pages
(60 weeks = 420 days) cover the 365-day window.

A record holds `days` from `start` (a Sunday) to `through` inclusive, where `through` is the Pacific date of `anchor_at`.
`anchor_stars` is the repo's total read by GraphQL seconds before its history pages, so the `through` day is partial (the
stars given before the anchor) and the anchor is the exact total at that instant. A batch of 100 waits for its request budget
before its anchor is read, never between the anchor and the reads, and a read that starts or ends more than 120 s after its
anchor is anchored and read again (`MAX_ANCHOR_AGE`): a star given in between would be counted twice. `pages` is how many
pages were read and `has_more` whether older weeks exist beyond `start` (they were not read). Sum of `days` = `anchor_stars` when `has_more` is
false (measured exact on 44 of 50 real repos, within 1 star on the other 6).

## `windows/latest.json`: the interface

```json
{
  "schema": "star-growth-dashboard/windows/1",
  "generated_at": "2026-10-30T05:31:12+00:00",
  "date": "2026-10-29",
  "threshold": 2000,
  "row_count": 33801,
  "first_snapshot_date": "2026-10-29",
  "windows": [
    {"days": 1, "column": "gain_1d", "source": "history", "exact_since": "2026-10-30", "baseline_date": null,
     "rows_snapshot": 0, "rows_history": 33790, "rows_null": 11},
    {"days": 7, "column": "gain_7d", "source": "history", "exact_since": "2026-11-05", "baseline_date": null, "...": "..."},
    {"days": 15, "...": "..."}, {"days": 30, "...": "..."}, {"days": 90, "...": "..."}, {"days": 180, "...": "..."}, {"days": 365, "...": "..."}
  ],
  "columns": {
    "node_id":    ["R_kgDOxxx", "..."],
    "full_name":  ["owner/name", "..."],
    "language":   ["Python", null, "..."],
    "created_at": ["2024-05-01T12:00:00Z", "..."],
    "stars":      [404185, "..."],
    "gain_1d":    [5120, "..."],  "gain_7d": ["..."], "gain_15d": ["..."], "gain_30d": ["..."], "gain_90d": ["..."], "gain_180d": ["..."], "gain_365d": ["..."],
    "history_mask": [0, 127, "..."]
  }
}
```

- **Columnar**: every array in `columns` has `row_count` entries; row *i* is one repo. Rows are the known repos whose count
  on `date` is above `threshold`, sorted by `stars` descending. A repo whose count could not be read that day is absent.
- **A gain over N days is the stars gained in the N × 24 hours ending at the capture** (`generated_at`), per row:
  - `source: snapshot`: today's count minus the count in the snapshot dated `date - N` (`baseline_date`), which the UTC
    cron captured N × 24 h earlier. Exact net change, unstars included. If that day's run failed, no older snapshot stands
    in (it would label a longer interval as N days): the window's `source` is `history` and `baseline_date` null that day.
  - `source: history`: the same interval from the history: the Pacific day holding the window start counted pro rata from
    that instant (its 23, 24 or 25 hours), the later days through the repo's anchor day, plus today's count minus the anchor
    count; kept within 0 and `stars`. Both sources cover the same instants, so a column does not jump when it flips
    (simulated at a constant 10 stars an hour, PDT, PST and across 2026-11-01: within 1 star). Stars given in the window and
    later removed are not counted (GitHub removes them from the history).
  - A window's `source` is `snapshot` when the snapshot dated `date - N` exists; `exact_since` is `first_snapshot_date + N`.
    The whole column flips on that day.
  - A row that the column's source cannot serve falls back to the other source: a repo discovered after the baseline
    snapshot uses history, and sets bit *i* of its `history_mask` (bit 0 = 1 d … bit 6 = 365 d; `127` = every window from
    history). A row neither source can serve (no history yet, an anchor older than the window start, or the window starting
    before the two pages read) is `null`.
  - History-sourced cells satisfy 0 ≤ `gain_1d` ≤ `gain_7d` ≤ … ≤ `gain_365d` ≤ `stars`; snapshot-sourced cells may be
    negative (unstars) and are never above `stars`.
    `rows_snapshot`, `rows_history` and `rows_null` per window count these.
- `node_id` is GitHub's GraphQL node ID: stable across renames (`full_name` follows the rename).
- `language` is the primary language or null; `created_at` is GitHub's ISO instant.

### Sample

`tests/fixtures/windows-sample.json` is a real 200-row extract in exactly this shape (same keys, same `windows` block; the
`rows_*` counts and `row_count` describe the extract).

## Budget and timing (measured 2026-10-29)

| step | requests | time |
|---|---|---|
| discovery, search healthy: ~60 slices of ≤ 800, 100 per page, 30 search requests/min | ~450–600 search | 15–20 min (research estimate; search was degraded all afternoon) |
| discovery, search degraded (every 100- and 50-result page 502 after ~10 s, half of the 30-result pages): 30 per page, 8 workers, pipelined slices | ~2,000 search at 29/min | ~340–400 repos/min, ~95 min for 33,889 repos (forks excluded then; 34,193 with them) |
| snapshot: `nodes(ids:)`, 100 per query, 4 workers | 320 points for 31,933 repos | 213 s, 0 failed queries |
| history for repos that lack it (today's entrants first), 2 pages each, ≤ 400 repos a day | ≤ 800 core | < 5 min |
| backfill (once, on the Mac): 2 pages × ~34k repos, ≤ 3,000 core/h, plus 1 anchor point per 100 repos | ~66k core + ~340 points | ~22 h, resumable (200 repos: 391 requests in 1.9 min) |

`GITHUB_TOKEN` allows 1,000 core requests and 1,000 GraphQL points an hour per repository, and 30 search requests a minute;
the daily run stays inside all three. Discovery stops at its deadline (75 min in the workflow) and reports which slices it
listed, failed or skipped; the snapshot of the known set never waits for it.
