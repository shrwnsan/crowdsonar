"""CLI helpers and end-to-end raw-store round trip (no LLM, no network)."""

from crowdsonar.cli import _raw_row_to_post
from crowdsonar.ingest import RawPost
from crowdsonar.storage import _raw_row_dict, save_raw_posts


def _post():
    return RawPost(
        source="arctic", post_id="1a", post_title="t", post_url="u", post_subreddit="s",
        post_author="a", post_score=5, post_created_utc=1760000000.0, post_body="b",
        comment_id="c1", comment_body="cb", comment_author="ca", comment_score=2,
        comment_created_utc=1760000001.0, keyword_match="kw",
    )


class TestRawRowRoundTrip:
    def test_dict_to_post_preserves_fields(self):
        p = _post()
        row = _raw_row_dict(p, run_id="run1", topic="topic")
        assert "ingested_at" in row

        back = _raw_row_to_post(row)
        assert back == p, "raw store round trip must reconstruct the RawPost exactly"

    def test_persist_then_reconstruct(self, data_dir):
        p = _post()
        save_raw_posts([p], "topic", "run1")

        import crowdsonar.storage as storage
        row = storage.load_raw_posts("topic")[0]
        assert _raw_row_to_post(row) == p
