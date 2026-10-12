"""Freshness-aware source routing — decide whether the real-time overlay runs.

`--source auto` used to be a static composition: archive always, PRAW overlay
whenever credentials were present. That wastes overlay quota on windows the
archive already covers, and it says nothing about WHY. The router makes the
decision explicit and testable:

- archive healthy and fresh  → overlay skipped (credential-free run)
- archive stale              → overlay tops up the recent tail
- archive empty or errored   → overlay becomes the fallback

Health here means data freshness, not just reachability: the archive is the
history tier, the overlay is the last-hours tier. `stale_after_hours` is the
threshold — default 48h, configurable per topic via
`sources.reddit.freshness_hours`.

Pure functions only — no I/O — so the decision table is exhaustively testable
and the CLI wiring stays thin.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_FRESHNESS_HOURS = 48.0


@dataclass
class FreshnessReport:
    """What the archive pass actually delivered, freshness-wise."""

    source: str = "arctic"
    items: int = 0
    errors: int = 0
    newest_item_utc: float | None = None  # max(post/comment created_utc)

    @property
    def age_hours(self) -> float | None:
        """Age of the newest item, or None when nothing came back."""
        if self.newest_item_utc is None:
            return None
        now = datetime.now(timezone.utc).timestamp()
        return max(0.0, (now - self.newest_item_utc) / 3600.0)


@dataclass
class RouterDecision:
    overlay: bool          # run the real-time overlay?
    reason: str            # human-readable why (logged + surfaced)
    fallback: bool = False # True when the archive effectively failed


def assess(posts: list, errors: list) -> FreshnessReport:
    """Summarize an archive IngestResult-shaped pass (posts, errors)."""
    timestamps = [p.post_created_utc for p in posts if p.post_created_utc]
    return FreshnessReport(
        items=len(posts),
        errors=len(errors),
        newest_item_utc=max(timestamps) if timestamps else None,
    )


def decide_overlay(report: FreshnessReport,
                   stale_after_hours: float = DEFAULT_FRESHNESS_HOURS) -> RouterDecision:
    """The routing table. One branch per (coverage, freshness) state."""
    age = report.age_hours

    if report.errors and report.items == 0:
        return RouterDecision(
            overlay=True, fallback=True,
            reason=f"archive errored ({report.errors} error(s)) — overlay becomes the source",
        )

    if report.items == 0 or age is None:
        return RouterDecision(
            overlay=True, fallback=True,
            reason="archive returned nothing for the window — overlay becomes the source",
        )

    if age > stale_after_hours:
        return RouterDecision(
            overlay=True,
            reason=(f"archive stale: newest item {age:.1f}h old > "
                    f"{stale_after_hours:g}h threshold — overlay tops up the recent tail"),
        )

    return RouterDecision(
        overlay=False,
        reason=(f"archive fresh: newest item {age:.1f}h old, within "
                f"{stale_after_hours:g}h threshold — overlay skipped"),
    )
