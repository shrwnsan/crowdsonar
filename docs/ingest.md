# Ingest sources, health, and provenance

CrowdSonar reads Reddit through a tiered source stack. Each tier has a
different cost/availability trade-off, and the pipeline is honest about which
tier every signal came from.

## Tier 1 — public archive (default)

`--source arctic` (and the archive half of `auto`).

The [Arctic Shift API](https://arctic-shift.photon-reddit.com) is a volunteer-run
public archive with a free, key-less JSON API. It needs no credentials, covers
full subreddit history, and costs nothing per request — which makes it the
right default for a research-scale listening pipeline. Two constraints shape
how we use it:

- **Politeness budget.** The service asks for roughly a couple of requests per
  second; `arctic_shift.py` throttles below that (`REQUEST_INTERVAL = 0.6`),
  caps pages per subreddit (`MAX_PAGES_PER_SUB = 20`), and retries 429 /
  query-timeout with backoff and a shrunk limit.
- **Single-operator risk.** It is one volunteer service. Bulk dumps trail the
  present by weeks, so recent-window reads ride the query API — and if the
  service is down, a run degrades to what's already banked. Mitigations: raw
  rows persist locally before any LLM call (crash-resume), and `crowdsonar
  doctor` reports reachability before you build a schedule on it.

## Tier 2 — official API overlay (PRAW, optional)

`--source praw` (forced), or the overlay under `auto` when the router asks for
it and `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are set.

The official Reddit API gives real-time listing (the last hours, where archive
coverage trails). It requires OAuth credentials and the API's terms price
research-scale reads, so CrowdSonar treats it as an optional overlay, never the
default path.

## Routing: what `--source auto` does

`auto` is a freshness-aware chain, not a fixed composition:

1. Ingest from the archive (Tier 1).
2. Probe coverage: one keyword-free query per subreddit (`fields=id`,
   `limit=1`) asking "does the archive hold ANY post newer than
   `sources.reddit.freshness_hours` (default 48h)?" This measures archive
   LAG, not keyword-matched item age — the ingest result cannot distinguish
   "archive is 50h behind" from "nobody used the keyword in 50h", and
   routing on matched-item age would fire the overlay forever on quiet
   topics.
3. Decide:

| Archive state | Overlay runs? | Fallback? | Why |
|---|---|---|---|
| Every subreddit covered within the threshold | **no** | no | credential-free run — the archive reaches the recent tail |
| Some subreddits uncovered | yes | no | overlay tops up the tail the archive missed |
| Partially covered + zero matched items + archive ingest errors | yes | **yes** | the archive effectively failed this pass |
| All coverage probes failed + zero matched items | yes | **yes** | coverage unverifiable and nothing delivered |

`fallback` means the archive effectively wasn't a source this pass. It only
changes how loud the run is — WARNING instead of INFO, ERROR when the overlay
is also wanted but credentials are missing — runs never fail solely because
the archive is down, but they don't get to look healthy either.

`--overlay always` restores the pre-router composition (overlay whenever
credentials are set, no probe cost) — the escape hatch if the probes
misbehave or you simply want the old behavior. Pre-0.2.0 setups that relied
on always-overlay should set this once and move on deliberately, not
silently.

The decision (and its reason) is logged every run, and each row's `source`
column preserves which tier delivered it — the briefing's ingest-mix line is
exactly this read-out. Health here means **coverage of the freshness
window**, not just reachability: the archive is the history tier, the overlay
is the last-hours tier. The decision table lives in `router.py` as pure
functions; the CLI wiring is deliberately thin.

## Tier 3 — session-based readers (documented, not shipped)

Tools that read social platforms through a logged-in browser session exist in
this ecosystem, and operators sometimes have one available. CrowdSonar
deliberately ships none of that: no session/cookie automation lives in this
repo, by policy. If you operate such a reader, the integration point is the
`RawPost` schema (`source` + post/comment fields) — map your reader's output to
`RawPost` and persist it into the raw store yourself, keeping the tool outside
this repository.

## Health: `crowdsonar doctor`

One command, one line per backend, exit code a cron/can wrap:

```
$ crowdsonar doctor
crowdsonar doctor — v0.1.2
  ok    arctic    reachable in 412ms (1 post sampled, quota remaining 99)
  warn  reddit    REDDIT_CLIENT_ID/SECRET not set — real-time overlay disabled
  ok    llm       endpoint authenticated (11 models visible)
  ok    storage   /path/data writable (3 signal file(s), 2 raw topic(s))

All required checks passed (reddit needs attention).
```

- `ok` — check passed.
- `warn` — degraded but runnable (e.g. overlay not configured).
- `FAIL` — a required backend is broken; **exit code 1**.

Probe semantics: single-attempt, 15s timeout — a doctor that retries for a
minute before reporting is worse than one that fails fast. The Reddit probe
lists one post through the real API path, so it validates credentials, not
just env-var presence.

## Provenance

Every raw and classified row carries a `source` column (`arctic` / `praw`),
and every synthesized briefing ends with a deterministic ingest-mix line:

```
---
*Ingest mix (past 7 days): arctic (public archive) 94% · praw (real-time) 6% — n=123 signals*
```

The line is computed from the stored rows after the LLM call, so it can't be
hallucinated by the model.
