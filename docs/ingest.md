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

`--source praw`, or the overlay half of `auto` when `REDDIT_CLIENT_ID` /
`REDDIT_CLIENT_SECRET` are set.

The official Reddit API gives real-time listing (the last hours, where archive
coverage trails). It requires OAuth credentials and the API's terms price
research-scale reads, so CrowdSonar treats it as an optional overlay, never the
default path. Set the credentials and it activates automatically in `auto`;
leave them unset and Tier 1 carries the run.

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
hallucinated by the model. Roadmap: freshness-aware source routing on top of
the same provenance signal (archive covers history, the overlay covers the
last hours — route accordingly, not statically).
