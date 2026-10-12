"""Freshness-aware source routing — decide whether the real-time overlay runs.

`--source auto` used to be a static composition: archive always, PRAW overlay
whenever credentials were present. That wastes overlay quota on windows the
archive already covers, and it says nothing about WHY. The router makes the
decision explicit and testable:

- archive covers the freshness window → overlay skipped (credential-free run)
- archive stale on some subreddits → overlay tops up the recent tail
- archive empty or errored → overlay becomes the source

Health here means DATA COVERAGE of the freshness window, not just
reachability. The signal is measured keyword-free: one cheap `fields=id`
query per subreddit asking "does the archive hold ANY post newer than the
threshold?" — because the keyword-matched ingest result cannot distinguish
"archive is 50h behind" from "nobody said the keyword in 50h", and routing on
matched-item age would fire the overlay forever on quiet topics.

Pure functions only — no I/O — so the decision table is exhaustively testable
and the CLI wiring stays thin.
"""

from dataclasses import dataclass

DEFAULT_FRESHNESS_HOURS = 48.0


@dataclass
class CoverageReport:
    """Archive coverage of the freshness window across the topic's subreddits."""

    subs_probed: int = 0      # probes that returned a definitive answer
    subs_covered: int = 0     # archive holds ≥1 post within the freshness window
    probe_errors: int = 0     # probes that failed outright (counted as uncovered)
    matched_items: int = 0    # keyword-matched items the archive pass collected
    ingest_errors: int = 0    # errors from the archive ingest pass


@dataclass
class RouterDecision:
    overlay: bool           # run the real-time overlay?
    fallback: bool = False  # True when the archive effectively ISN'T a source this pass
    reason: str = ""        # human-readable why (logged every run)


def decide_overlay(report: CoverageReport,
                   stale_after_hours: float = DEFAULT_FRESHNESS_HOURS) -> RouterDecision:
    """The routing table. One branch per (coverage, verification) state."""
    threshold = f"{stale_after_hours:g}h"

    # Probes all failed — coverage unverifiable. Fall back to what the ingest
    # pass itself delivered: items in hand means stale-top-up; nothing means
    # the archive is effectively down.
    if report.subs_probed == 0:
        return RouterDecision(
            overlay=True, fallback=report.matched_items == 0,
            reason=("coverage unverifiable (all probes failed) — overlay "
                    f"{'is the source' if report.matched_items == 0 else 'tops up'}"),
        )

    if report.subs_covered >= report.subs_probed:
        return RouterDecision(
            overlay=False,
            reason=(f"archive fresh: {report.subs_covered}/{report.subs_probed} subreddits "
                    f"have items within the {threshold} threshold — overlay skipped"),
        )

    missing = report.subs_probed - report.subs_covered
    # The archive is effectively absent this pass only when it delivered
    # neither coverage nor matched items.
    fallback = report.matched_items == 0 and (report.subs_covered == 0 or report.ingest_errors > 0)
    detail = (f"{missing}/{report.subs_probed} subreddit(s) have no archive data within "
              f"{threshold}")
    if report.probe_errors:
        detail += f" ({report.probe_errors} probe error(s) counted as uncovered)"
    return RouterDecision(
        overlay=True, fallback=fallback,
        reason=f"archive stale: {detail} — overlay tops up the recent tail",
    )
