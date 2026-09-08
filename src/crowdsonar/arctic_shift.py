"""Arctic Shift API adapter — credential-free Reddit ingestion (primary source).

Replaces PullPush (dead 2026-09) as the default ingestion path. No OAuth, no
API key. See docs/architecture.md §2 for the source-selection rationale.

Strategy: fetch the full post window per subreddit (server-side time filter,
paginated), then filter by keywords locally using the same `_matches_keywords`
semantics as the PRAW path. Server-side keyword search (`title=`/`selftext=`/
`body=` params) is deliberately avoided — it times out on active subreddits
and the API docs mark its behavior as "not guaranteed".

Comment recovery mirrors PRAW: for each keyword-matched post, fetch comments
via `link_id`, sort by score locally, keep the top `comments_per_post`.

Be polite: this is a free volunteer-run service. Requests are throttled below
"a couple per second" (their stated safe zone); 429 and query-timeout get
retried with backoff and a shrunk limit.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .ingest import IngestResult, RawPost, _matches_keywords

log = logging.getLogger(__name__)

BASE_URL = "https://arctic-shift.photon-reddit.com"
POST_FIELDS = "id,title,selftext,subreddit,author,score,created_utc"
COMMENT_FIELDS = "id,body,author,score,created_utc,link_id"
PAGE_SIZE = 100           # explicit (not "auto") so cursor pagination is predictable
MAX_PAGES_PER_SUB = 20    # safety cap: 20 × 100 = 2000 posts per sub per window
REQUEST_INTERVAL = 0.6    # seconds between requests — inside their "couple/sec" tolerance

TIME_FILTER_DAYS = {
    "hour": 1 / 24,
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
    "all": None,
}


def _iso(ts: float) -> str:
    """Epoch seconds → ISO string in the format the API accepts (validated)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ArcticShiftClient:
    """Throttled wrapper over the Arctic Shift search endpoints."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0,
                 request_interval: float = REQUEST_INTERVAL):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._interval = request_interval
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def search(self, kind: str, params: dict) -> list[dict]:
        """GET /api/{kind}/search with 429 / query-timeout retry. Returns data list."""
        p = dict(params)
        path = f"/api/{kind}/search"
        for attempt in range(3):
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._http.get(path, params=p)
            except httpx.HTTPError as e:
                log.warning("Arctic Shift %s: %s (attempt %d/3)", path, e, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                reset = float(resp.headers.get("X-RateLimit-Reset") or 30)
                log.warning("Arctic Shift: rate limited, sleeping %.0fs", min(reset, 120))
                time.sleep(min(reset, 120))
                continue
            resp.raise_for_status()
            body = resp.json()
            err = body.get("error")
            if err:
                msg = str(err)
                if "imeout" in msg:  # "Query timed out" / "Timeout. Maybe slow down a bit"
                    p = dict(p)
                    lim = p.get("limit", PAGE_SIZE)
                    # "auto" (comment search) has no numeric half — drop to 100.
                    p["limit"] = max(10, lim // 2) if isinstance(lim, int) else 100
                    log.warning("Arctic Shift: query timed out — retrying with limit=%s", p["limit"])
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Arctic Shift error: {msg}")
            return body.get("data") or []
        return []  # retries exhausted; caller sees a short/empty page


def fetch_window_posts(client: ArcticShiftClient, subreddit: str,
                       window_days: float | None) -> list[dict]:
    """All posts in r/<subreddit> within `window_days` (None = no lower bound)."""
    items: list[dict] = []
    cursor: float | None = None
    for _ in range(MAX_PAGES_PER_SUB):
        params = {
            "subreddit": subreddit,
            "limit": PAGE_SIZE,
            "sort": "asc",
            "fields": POST_FIELDS,
        }
        if cursor is not None:
            params["after"] = _iso(cursor)
        elif window_days is not None:
            params["after"] = _iso(
                (datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()
            )
        batch = client.search("posts", params)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        new_cursor = float(batch[-1].get("created_utc") or 0)
        if cursor is not None and new_cursor <= cursor:
            break  # cursor stalled (same-second pileup); dedup handles overlap
        cursor = new_cursor
    return items


def fetch_post_comments(client: ArcticShiftClient, post_id: str) -> list[dict]:
    """Comments under a post (up to ~1000 via limit=auto); caller sorts/filters."""
    return client.search("comments", {
        "link_id": f"t3_{post_id}",
        "limit": "auto",
        "fields": COMMENT_FIELDS,
    })


def _post_raw(item: dict, kw: str) -> RawPost:
    pid = item.get("id", "")
    sub = item.get("subreddit", "")
    return RawPost(
        source="arctic",
        post_id=pid,
        post_title=item.get("title", ""),
        post_url=f"https://reddit.com/r/{sub}/comments/{pid}",
        post_subreddit=sub,
        post_author=item.get("author") or "[deleted]",
        post_score=int(item.get("score") or 0),
        post_created_utc=float(item.get("created_utc") or 0),
        post_body=item.get("selftext") or "",
        keyword_match=kw,
    )


def _comment_raw(item: dict, parent: RawPost, kw: str) -> RawPost:
    return RawPost(
        source="arctic",
        post_id=parent.post_id,
        post_title=parent.post_title,
        post_url=parent.post_url,
        post_subreddit=parent.post_subreddit,
        post_author=parent.post_author,
        post_score=parent.post_score,
        post_created_utc=parent.post_created_utc,
        post_body=parent.post_body,
        comment_id=item.get("id", ""),
        comment_body=item.get("body") or "",
        comment_author=item.get("author") or "[deleted]",
        comment_score=int(item.get("score") or 0),
        comment_created_utc=float(item.get("created_utc") or 0),
        keyword_match=kw,
    )


def ingest_arctic(
    subreddits: list[str],
    keywords: list[str],
    time_filter: str = "week",
    post_limit: int = 50,
    comments_per_post: int = 20,
    backfill_days: int = 0,
    base_url: str = BASE_URL,
) -> IngestResult:
    """Ingest via Arctic Shift — semantics mirror ingest_praw.

    Posts matching any keyword (title or body, case-insensitive substring)
    within the time window, plus the top `comments_per_post` comments (by
    score) of each matched post. `backfill_days > 0` widens the window
    (replaces the dead PullPush backfill). `post_limit` caps matched posts
    per subreddit (newest kept).
    """
    result = IngestResult()
    window = backfill_days if backfill_days > 0 else TIME_FILTER_DAYS.get(time_filter, 7)
    client = ArcticShiftClient(base_url=base_url)
    try:
        for sub in subreddits:
            try:
                items = fetch_window_posts(client, sub, window)
            except Exception as e:
                result.errors.append(f"posts r/{sub}: {e}")
                continue

            # Pagination can straddle a boundary second — dedupe by post id.
            seen: set[str] = set()
            unique = [x for x in items
                      if not ((x.get("id") or "") in seen or seen.add(x.get("id") or ""))]
            log.info("Arctic Shift: r/%s → %d posts in %.1f-day window",
                     sub, len(unique), window if window is not None else -1)

            matched: list[tuple[dict, str]] = []
            for it in unique:
                kw = _matches_keywords(
                    (it.get("title") or "") + " " + (it.get("selftext") or ""), keywords
                )
                if kw:
                    matched.append((it, kw))

            if post_limit and len(matched) > post_limit:
                matched.sort(key=lambda x: x[0].get("created_utc") or 0, reverse=True)
                matched = matched[:post_limit]

            for it, kw in matched:
                parent = _post_raw(it, kw)
                result.posts.append(parent)
                if comments_per_post > 0:
                    try:
                        cs = fetch_post_comments(client, it.get("id", ""))
                    except Exception as e:
                        result.errors.append(f"comments t3_{it.get('id')}: {e}")
                        cs = []
                    cs.sort(key=lambda c: c.get("score") or 0, reverse=True)
                    for c in cs[:comments_per_post]:
                        result.posts.append(_comment_raw(c, parent, kw))
    finally:
        client.close()

    log.info("Arctic Shift: collected %d items, %d errors", len(result.posts), len(result.errors))
    return result
