"""Row builders shared across test modules."""

from datetime import datetime, timezone

from crowdsonar.ingest import RawPost


def make_post(source="arctic", post_id="1a", comment_id="", score=10):
    return RawPost(
        source=source,
        post_id=post_id,
        post_title="title",
        post_url="https://reddit.com/r/s/comments/1a",
        post_subreddit="s",
        post_author="a",
        post_score=score,
        post_created_utc=1760000000.0,
        post_body="body",
        comment_id=comment_id,
        comment_body="cbody" if comment_id else "",
        comment_author="ca" if comment_id else "",
        comment_score=score,
        comment_created_utc=1760000000.0,
        keyword_match="kw",
    )


def make_signal_row(raw_dict, signal_type="product_launch"):
    """Minimal classified row matching storage._signals_schema()."""
    row = dict(raw_dict)
    row.update({
        "run_id": "run1",
        "topic": "topic",
        "signal_type": signal_type,
        "sentiment": "positive",
        "strength": "notable",
        "confirmation_status": "self_announced",
        "entities": '["Acme"]',
        "summary": "Acme launched a thing",
        "classified_at": datetime.now(timezone.utc).isoformat(),
    })
    return row
