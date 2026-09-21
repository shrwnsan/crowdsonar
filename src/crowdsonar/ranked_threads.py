"""Ranked-threads store — persistent top-N thread tracking.

Threads are upserted per topic into data/ranked_threads/{topic}.parquet,
deduped on thread_id: re-sighting a thread updates position/last_seen while
first_seen is preserved from first sight. Until a real crawler exists,
threads enter the store via the CLI --seo-file escape hatch (a JSON list of
thread dicts) and are rendered into the digest with --seo.
"""

import logging
from datetime import datetime, timezone

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from crowdsonar.storage import DATA_DIR

log = logging.getLogger(__name__)

RANKED_THREADS_DIR = DATA_DIR / "ranked_threads"


def _thread_schema() -> pa.Schema:
    return pa.schema([
        ("thread_id", pa.string()),
        ("keyword", pa.string()),
        ("url", pa.string()),
        ("position", pa.int64()),
        ("age_days", pa.int64()),        # nullable
        ("comments", pa.int64()),        # nullable
        ("competitor_named", pa.bool_()),
        ("first_seen", pa.string()),     # ISO-8601 UTC
        ("last_seen", pa.string()),      # ISO-8601 UTC
    ])


def _normalize_thread(t: dict, now: str) -> dict:
    """Fill missing fields with defaults; returns a schema-exact row."""
    def _int_or_none(v) -> int | None:
        return int(v) if v is not None else None

    return {
        "thread_id": str(t.get("thread_id", "")),
        "keyword": str(t.get("keyword", "")),
        "url": str(t.get("url", "")),
        "position": int(t.get("position", 0) or 0),
        "age_days": _int_or_none(t.get("age_days")),
        "comments": _int_or_none(t.get("comments")),
        "competitor_named": bool(t.get("competitor_named", False)),
        "first_seen": now,   # set once at first sight (upsert preserves it)
        "last_seen": now,
    }


def upsert_threads(topic: str, threads: list[dict]) -> int:
    """Upsert threads for a topic, deduped on thread_id.

    Re-sighting updates position/age_days/comments/competitor_named/url and
    last_seen; first_seen is preserved. Returns the number of rows written.
    """
    if not threads:
        log.warning("No threads to upsert for topic '%s'", topic)
        return 0

    now = datetime.now(timezone.utc).isoformat()

    existing: dict[str, dict] = {r["thread_id"]: r for r in load_threads(topic)}

    # Seed with the existing store so a partial batch never evicts threads
    # it didn't re-sight; incoming threads overlay on top.
    merged: dict[str, dict] = dict(existing)
    for t in threads:
        if not t.get("thread_id"):
            log.warning("Skipping thread without thread_id: %s", t.get("url", t))
            continue
        row = _normalize_thread(t, now)
        prior = existing.get(row["thread_id"])
        if prior is not None:
            row["first_seen"] = prior["first_seen"]
        merged[row["thread_id"]] = row

    if not merged:
        return 0

    rows = sorted(merged.values(), key=lambda r: r["position"])
    RANKED_THREADS_DIR.mkdir(parents=True, exist_ok=True)
    path = RANKED_THREADS_DIR / f"{topic}.parquet"
    table = pa.Table.from_pylist(rows, schema=_thread_schema())
    pq.write_table(table, path, compression="zstd")
    log.info("Ranked threads: %d upserted -> %s", len(rows), path)
    return len(rows)


def load_threads(topic: str) -> list[dict]:
    """Load ranked threads for a topic via DuckDB, ordered by position."""
    con = duckdb.connect(":memory:")
    path = RANKED_THREADS_DIR / f"{topic}.parquet"
    try:
        result = con.execute(
            f"SELECT * FROM read_parquet('{path}') ORDER BY position"
        ).fetchall()
    except Exception:
        log.info("No ranked threads for topic '%s' in %s", topic, RANKED_THREADS_DIR)
        con.close()
        return []
    columns = [d[0] for d in con.description]
    con.close()
    return [dict(zip(columns, row)) for row in result]


def format_threads_table(rows: list[dict], limit: int = 10) -> str:
    """Render ranked threads as a plain-text table (digest-friendly).

    Format: position · keyword · url · competitor flag, with age/comments
    appended when present.
    """
    if not rows:
        return "No ranked threads on file."
    lines = []
    for r in rows[:limit]:
        label = r.get("keyword") or "—"
        link = r.get("url") or r.get("thread_id") or "—"
        competitor = "competitor" if r.get("competitor_named") else "organic"
        cells = [f"#{int(r.get('position') or 0)}", label, link, competitor]
        if r.get("age_days") is not None:
            cells.append(f"age {r['age_days']}d")
        if r.get("comments") is not None:
            cells.append(f"{r['comments']} comments")
        lines.append(" · ".join(str(c) for c in cells))
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more")
    return "\n".join(lines)
