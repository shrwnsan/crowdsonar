"""Arctic Shift adapter: response mapping, pagination, filtering, retries.

Network-free: httpx is driven through MockTransport and time.sleep is a no-op.
"""

import httpx
import pytest

import crowdsonar.arctic_shift as arctic_shift
from crowdsonar.arctic_shift import (
    ArcticShiftClient,
    PAGE_SIZE,
    _comment_raw,
    _iso,
    _post_raw,
    fetch_window_posts,
    ingest_arctic,
)

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(arctic_shift.time, "sleep", lambda s: None)


class TestMapping:
    def test_iso_format(self):
        assert _iso(1760000000.0) == "2025-10-09 08:53:20"

    def test_post_raw_maps_fields(self, arctic_posts_page):
        raw = _post_raw(arctic_posts_page[0], "exam")
        assert raw.source == "arctic"
        assert raw.post_id == "1abc001"
        assert raw.post_url == "https://reddit.com/r/exampletopic/comments/1abc001"
        assert raw.post_score == 42
        assert raw.keyword_match == "exam"

    def test_post_raw_deleted_and_bad_score(self):
        raw = _post_raw({"id": "x", "subreddit": "s", "author": None, "score": None,
                         "created_utc": None, "title": "t", "selftext": None}, "kw")
        assert raw.post_author == "[deleted]"
        assert raw.post_score == 0
        assert raw.post_created_utc == 0.0
        assert raw.post_body == ""

    def test_comment_raw_inherits_parent(self, arctic_posts_page, arctic_comments_page):
        parent = _post_raw(arctic_posts_page[2], "exam")
        c = _comment_raw(arctic_comments_page[0], parent, "exam")
        assert c.comment_id == "c1"
        assert c.post_id == parent.post_id
        assert c.post_title == parent.post_title
        assert c.comment_score == 21


class FakeClient:
    """Duck-typed stand-in for ArcticShiftClient.search."""

    def __init__(self, pages_by_kind, calls=None):
        self.pages_by_kind = pages_by_kind
        self.calls = calls if calls is not None else []

    def search(self, kind, params):
        self.calls.append((kind, params))
        pages = self.pages_by_kind.get(kind) or []
        return pages.pop(0) if pages else []

    def close(self):
        pass


class TestPagination:
    def test_short_first_page_stops(self, arctic_posts_page):
        client = FakeClient({"posts": [arctic_posts_page]})
        items = fetch_window_posts(client, "exampletopic", 7)
        assert len(items) == 4  # fixture page is below PAGE_SIZE → single page
        assert client.calls[0][1]["subreddit"] == "exampletopic"

    def test_full_pages_follow_cursor(self, arctic_posts_page):
        page_a = [dict(p, created_utc=float(i)) for i, p in enumerate(arctic_posts_page * 25)]
        page_b = [dict(arctic_posts_page[0], id="late1", created_utc=99999.0)]
        client = FakeClient({"posts": [page_a, page_b]})
        items = fetch_window_posts(client, "exampletopic", 7)
        assert items[-1]["id"] == "late1"
        assert "after" in client.calls[1][1], "second page must carry the cursor"

    def test_cursor_stall_stops_and_overlaps(self, arctic_posts_page):
        page_a = [dict(p, created_utc=float(i)) for i, p in enumerate(arctic_posts_page * 25)]
        stall = [dict(arctic_posts_page[0], id="stall", created_utc=24.0)]  # <= last cursor of page_a
        client = FakeClient({"posts": [page_a, stall]})
        items = fetch_window_posts(client, "exampletopic", 7)
        assert len(client.calls) == 2, "stalled cursor must end pagination"
        # The stall page is still returned — overlapping ids are the caller's
        # dedupe problem (ingest_arctic dedupes by post id).
        assert len(items) == len(page_a) + len(stall)
        assert items[-1]["id"] == "stall"

    def test_no_window_when_all(self, arctic_posts_page):
        client = FakeClient({"posts": [arctic_posts_page]})
        fetch_window_posts(client, "exampletopic", None)
        assert "after" not in client.calls[0][1]


class TestIngestArctic:
    def test_filter_dedupe_limit_comments(self, arctic_posts_page, arctic_comments_page, monkeypatch):
        client = FakeClient({"posts": [arctic_posts_page]})
        monkeypatch.setattr(arctic_shift, "ArcticShiftClient", lambda base_url: client)
        monkeypatch.setattr(arctic_shift, "fetch_post_comments",
                            lambda c, post_id: arctic_comments_page)

        result = ingest_arctic(
            subreddits=["exampletopic"],
            keywords=["exam", "certified"],
            time_filter="week",
            post_limit=1,            # newest matched post kept
            comments_per_post=2,     # top-2 by score
        )

        ids = [(p.post_id, p.comment_id) for p in result.posts]
        # 1abc003 wins post_limit (newest); 1abc001 is dropped; dup 1abc003 deduped
        assert ids == [("1abc003", ""), ("1abc003", "c3"), ("1abc003", "c1")]
        assert all(p.source == "arctic" for p in result.posts)
        assert result.errors == []

    def test_errors_surface_per_sub(self, monkeypatch):
        class BoomClient:
            def search(self, kind, params):
                raise RuntimeError("boom")

            def close(self):
                pass

        monkeypatch.setattr(arctic_shift, "ArcticShiftClient", lambda base_url: BoomClient())
        result = ingest_arctic(subreddits=["quiet"], keywords=["x"], time_filter="week")
        assert result.posts == []
        assert len(result.errors) == 1
        assert "r/quiet" in result.errors[0]


class TestRetries:
    """The client's retry ladder, exercised through a mock transport."""

    def _client(self, handler):
        c = ArcticShiftClient(request_interval=0.0)
        c._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
        return c

    def test_success_returns_data(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "1"}]})

        assert self._client(handler).search("posts", {}) == [{"id": "1"}]

    def test_429_then_success(self):
        codes = iter([429, 200])

        def handler(request):
            code = next(codes)
            if code == 429:
                return httpx.Response(429, headers={"X-RateLimit-Reset": "1"})
            return httpx.Response(200, json={"data": []})

        c = self._client(handler)
        assert c.search("posts", {}) == []
        assert c._last > 0  # throttle bookkeeping survived the retry

    def test_timeout_error_shrinks_limit(self):
        seen_limits = []

        def handler(request):
            seen_limits.append(request.url.params.get("limit"))
            if len(seen_limits) == 1:
                return httpx.Response(200, json={"error": "Query timed out"})
            return httpx.Response(200, json={"data": [{"id": "1"}]})

        c = self._client(handler)
        assert c.search("posts", {"limit": PAGE_SIZE}) == [{"id": "1"}]
        assert int(seen_limits[1]) == PAGE_SIZE // 2

    def test_hard_error_raises(self):
        def handler(request):
            return httpx.Response(200, json={"error": "bad params"})

        with pytest.raises(RuntimeError, match="bad params"):
            self._client(handler).search("posts", {})

    def test_422_blip_recovers(self):
        codes = iter([422, 422, 200])

        def handler(request):
            code = next(codes)
            if code == 422:
                return httpx.Response(422)
            return httpx.Response(200, json={"data": [{"id": "1"}]})

        assert self._client(handler).search("posts", {"limit": 10}) == [{"id": "1"}]

    def test_5xx_exhausts_and_raises(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503)

        with pytest.raises(httpx.HTTPStatusError):
            self._client(handler).search("posts", {})
        assert calls["n"] == 3
