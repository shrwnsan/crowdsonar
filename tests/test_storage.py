"""Parquet store: raw dedupe, classification delta, signal queries."""

import crowdsonar.storage as storage
from crowdsonar.storage import (
    get_signal_counts,
    load_raw_posts,
    pending_classifications,
    query_signals,
    save_raw_posts,
    save_signals,
)
from helpers import make_post as _post
from helpers import make_signal_row as _signal_row


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

    def test_cross_source_twins_classified_once(self, data_dir):
        """Archive outage banks a praw row; recovery banks its arctic twin.

        The item must classify exactly once — arctic twin preferred while
        both are unclassified, and a praw-side classification satisfies the
        arctic twin once it arrives.
        """
        save_raw_posts([_post(source="praw")], "topic", "run1")
        save_raw_posts([_post(source="arctic")], "topic", "run2")

        pending = pending_classifications("topic")
        assert [p["source"] for p in pending] == ["arctic"], "arctic twin wins the slot"

        save_signals([_signal_row(p) for p in pending], "topic", "run1")
        assert pending_classifications("topic") == []

    def test_praw_side_classification_satisfies_arctic_twin(self, data_dir):
        save_raw_posts([_post(source="praw")], "topic", "run1")
        classified = pending_classifications("topic")  # praw only — no twin yet
        save_signals([_signal_row(p) for p in classified], "topic", "run1")

        save_raw_posts([_post(source="arctic")], "topic", "run2")
        assert pending_classifications("topic") == [], "item identity is (post_id, comment_id), not source"

    def test_unrelated_items_still_independent(self, data_dir):
        save_raw_posts([_post(post_id="1a")], "topic", "run1")
        save_raw_posts([_post(post_id="1a", comment_id="c1")], "topic", "run2")
        assert len(pending_classifications("topic")) == 2, "comment rows are distinct items"


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
