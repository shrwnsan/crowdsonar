"""Contract tests for the shared RawPost shape and keyword matching."""

from crowdsonar.ingest import IngestResult, RawPost, _matches_keywords


class TestKeywordMatching:
    def test_case_insensitive_hit(self):
        assert _matches_keywords("Big EXAM tomorrow", ["exam"]) == "exam"

    def test_first_keyword_wins(self):
        assert _matches_keywords("passed my exam, now certified", ["certified", "exam"]) == "certified"

    def test_miss_returns_none(self):
        assert _matches_keywords("unrelated text", ["exam"]) is None

    def test_empty_keywords(self):
        assert _matches_keywords("anything", []) is None


class TestRawPostShape:
    def test_comment_fields_default_to_post_only(self):
        p = RawPost(
            source="arctic",
            post_id="1a",
            post_title="t",
            post_url="u",
            post_subreddit="s",
            post_author="a",
            post_score=1,
            post_created_utc=0.0,
            post_body="b",
        )
        assert p.comment_id == ""
        assert p.comment_body == ""
        assert p.keyword_match == ""

    def test_ingest_result_defaults(self):
        r = IngestResult()
        assert r.posts == []
        assert r.errors == []
        assert r.rate_limit_hits == 0
