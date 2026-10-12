"""Parquet store: raw dedupe, classification delta, signal queries."""

from datetime import datetime, timezone

import crowdsonar.storage as storage
from crowdsonar.ingest import RawPost
from crowdsonar.storage import (
    get_signal_counts,
    load_raw_posts,
    pending_classifications,
    query_signals,
    save_raw_posts,
    save_signals,
)


def _post(source="arctic", post_id="1a", comment_id="", score=10):
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


def _signal_row(raw_dict, signal_type="product_launch"):
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


class TestRawStore:
    def test_save_dedupes_across_runs(self, data_dir):
        posts = [_post(), _post(post_id="1b", comment_id="c1")]
        _, new1 = save_raw_posts(posts, "topic", "run1")
        assert new1 == 2

        _, new2 = save_raw_posts(posts, "topic", "run2")
        assert new2 == 0, "re-ingesting the same window must be idempotent"

    def test_dedupe_key_includes_source(self, data_dir):
        save_raw_posts([_post(source="arctic")], "topic", "run1")
        _, new = save_raw_posts([_post(source="praw")], "topic", "run2")
        assert new == 1, "same ids from a different backend are new rows"
        sources = {r["source"] for r in load_raw_posts("topic")}
        assert sources == {"arctic", "praw"}

    def test_load_raw_posts_empty(self, data_dir):
        assert load_raw_posts("nothing") == []


class TestClassificationDelta:
    def test_pending_then_clear(self, data_dir):
        posts = [_post(), _post(post_id="1b", comment_id="c1")]
        save_raw_posts(posts, "topic", "run1")

        pending = pending_classifications("topic")
        assert len(pending) == 2

        save_signals([_signal_row(p) for p in pending], "topic", "run1")
        assert pending_classifications("topic") == []


class TestSignalQueries:
    def _seed(self, data_dir):
        rows = [
            _signal_row({"source": "arctic", "post_id": "1a", "comment_id": "",
                         "post_score": 30, "post_title": "T1"}),
            _signal_row({"source": "praw", "post_id": "1b", "comment_id": "",
                         "post_score": 20, "post_title": "T2"}, signal_type="noise"),
        ]
        return save_signals(rows, "topic", "run1")

    def test_query_round_trip_and_order(self, data_dir):
        self._seed(data_dir)
        rows = query_signals(topic="topic")
        assert [r["post_title"] for r in rows] == ["T1", "T2"]  # post_score DESC
        assert rows[0]["entities"] == ["Acme"], "entities JSON string decodes to list"
        assert rows[0]["source"] == "arctic"

    def test_query_filters(self, data_dir):
        self._seed(data_dir)
        assert [r["post_title"] for r in query_signals(topic="topic", signal_type="noise")] == ["T2"]
        assert query_signals(topic="nonexistent") == []  # no parquet yet for that topic

    def test_signal_counts(self, data_dir):
        self._seed(data_dir)
        counts = get_signal_counts("topic")
        assert counts == {"product_launch": 1, "noise": 1}
