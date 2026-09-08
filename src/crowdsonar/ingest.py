"""Reddit ingestion — PRAW real-time overlay.

Primary source is the Arctic Shift API (src/crowdsonar/arctic_shift.py):
credential-free, no OAuth. PullPush was removed 2026-09 (API dead);
docs/architecture.md records the rationale.
"""

import logging
import time
from dataclasses import dataclass, field

import httpx
from praw import Reddit
from praw.models import MoreComments

log = logging.getLogger(__name__)


@dataclass
class RawPost:
    """A single Reddit post or comment, pre-classification."""
    source: str  # "arctic" or "praw"
    post_id: str
    post_title: str
    post_url: str
    post_subreddit: str
    post_author: str
    post_score: int
    post_created_utc: float
    post_body: str

    # Comment-level (empty string if this is the post itself)
    comment_id: str = ""
    comment_body: str = ""
    comment_author: str = ""
    comment_score: int = 0
    comment_created_utc: float = 0.0

    # Metadata
    keyword_match: str = ""  # which keyword triggered the match


@dataclass
class IngestResult:
    posts: list[RawPost] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rate_limit_hits: int = 0


def create_praw_client(client_id: str, client_secret: str, user_agent: str) -> Reddit:
    """Create a PRAW Reddit instance from credentials."""
    return Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def _matches_keywords(text: str, keywords: list[str]) -> str | None:
    """Return the first matching keyword (lowered), or None."""
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return kw.lower()
    return None


def ingest_praw(
    reddit: Reddit,
    subreddits: list[str],
    keywords: list[str],
    sort: str = "new",
    time_filter: str = "week",
    post_limit: int = 50,
    comments_per_post: int = 20,
) -> IngestResult:
    """Ingest recent posts + comments from subreddits via PRAW."""
    result = IngestResult()
    sub_str = "+".join(subreddits)

    log.info("PRAW: searching r/%s (sort=%s, time=%s, limit=%d)",
             sub_str, sort, time_filter, post_limit)

    try:
        sub = reddit.subreddit(sub_str)
    except Exception as e:
        result.errors.append(f"Failed to access subreddit: {e}")
        return result

    try:
        submissions = list(getattr(sub, sort)(time_filter=time_filter, limit=post_limit))
    except Exception as e:
        result.errors.append(f"Failed to list submissions: {e}")
        return result

    for submission in submissions:
        # Check if post body or title matches keywords
        kw = _matches_keywords(submission.title + " " + (submission.selftext or ""), keywords)
        if kw:
            result.posts.append(RawPost(
                source="praw",
                post_id=submission.id,
                post_title=submission.title,
                post_url=f"https://reddit.com{submission.permalink}",
                post_subreddit=str(submission.subreddit),
                post_author=str(submission.author) if submission.author else "[deleted]",
                post_score=submission.score,
                post_created_utc=submission.created_utc,
                post_body=submission.selftext or "",
                keyword_match=kw,
            ))

        # Fetch top comments
        if comments_per_post > 0:
            try:
                submission.comments.replace_more(limit=0)  # skip "load more" for speed
            except Exception:
                pass

            for comment in submission.comments[:comments_per_post]:
                if isinstance(comment, MoreComments):
                    continue
                kw_c = _matches_keywords(comment.body or "", keywords)
                if kw_c:
                    result.posts.append(RawPost(
                        source="praw",
                        post_id=submission.id,
                        post_title=submission.title,
                        post_url=f"https://reddit.com{submission.permalink}",
                        post_subreddit=str(submission.subreddit),
                        post_author=str(submission.author) if submission.author else "[deleted]",
                        post_score=submission.score,
                        post_created_utc=submission.created_utc,
                        post_body=submission.selftext or "",
                        comment_id=comment.id,
                        comment_body=comment.body or "",
                        comment_author=str(comment.author) if comment.author else "[deleted]",
                        comment_score=comment.score,
                        comment_created_utc=comment.created_utc,
                        keyword_match=kw_c,
                    ))

        # PRAW rate limit courtesy
        time.sleep(0.5)

    log.info("PRAW: collected %d matching items", len(result.posts))
    return result


