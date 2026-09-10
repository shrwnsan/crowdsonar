"""DuckDB + Parquet storage for classified signals."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

# Data lives relative to the working directory (never inside site-packages
# once installed); override with CROWDSONAR_DATA_DIR.
DATA_DIR = Path(os.environ.get("CROWDSONAR_DATA_DIR", Path.cwd() / "data"))
SIGNALS_DIR = DATA_DIR / "signals"
RAW_DIR = DATA_DIR / "raw"
TOPICS_DIR = DATA_DIR / "topics"


def _signals_schema() -> pa.Schema:
    return pa.schema([
        ("run_id", pa.string()),
        ("topic", pa.string()),
        ("source", pa.string()),         # praw / pullpush
        ("post_id", pa.string()),
        ("post_title", pa.string()),
        ("post_url", pa.string()),
        ("post_subreddit", pa.string()),
        ("post_author", pa.string()),
        ("post_score", pa.int64()),
        ("post_created_utc", pa.float64()),
        ("post_body", pa.string()),
        ("comment_id", pa.string()),
        ("comment_body", pa.string()),
        ("comment_author", pa.string()),
        ("comment_score", pa.int64()),
        ("comment_created_utc", pa.float64()),
        ("keyword_match", pa.string()),
        # Classification fields
        ("signal_type", pa.string()),
        ("sentiment", pa.string()),
        ("strength", pa.string()),
        ("confirmation_status", pa.string()),  # rumor / self_announced / officially_confirmed
        ("entities", pa.string()),       # JSON list
        ("summary", pa.string()),        # one-line LLM summary
        ("classified_at", pa.string()),  # ISO timestamp
    ])


def _raw_schema() -> pa.Schema:
    """Raw store: classified schema minus the 6 classification fields, plus ingested_at (18 fields).

    Classification-free by construction — safe to persist before any LLM call.
    """
    return pa.schema([
        ("run_id", pa.string()),
        ("topic", pa.string()),
        ("source", pa.string()),
        ("post_id", pa.string()),
        ("post_title", pa.string()),
        ("post_url", pa.string()),
        ("post_subreddit", pa.string()),
        ("post_author", pa.string()),
        ("post_score", pa.int64()),
        ("post_created_utc", pa.float64()),
        ("post_body", pa.string()),
        ("comment_id", pa.string()),
        ("comment_body", pa.string()),
        ("comment_author", pa.string()),
        ("comment_score", pa.int64()),
        ("comment_created_utc", pa.float64()),
        ("keyword_match", pa.string()),
        ("ingested_at", pa.string()),    # ISO timestamp — set at persist time
    ])


def _row_dict(raw, classification: dict, run_id: str, topic: str) -> dict:
    return {
        "run_id": run_id,
        "topic": topic,
        "source": raw.source,
        "post_id": raw.post_id,
        "post_title": raw.post_title,
        "post_url": raw.post_url,
        "post_subreddit": raw.post_subreddit,
        "post_author": raw.post_author,
        "post_score": raw.post_score,
        "post_created_utc": raw.post_created_utc,
        "post_body": raw.post_body,
        "comment_id": raw.comment_id,
        "comment_body": raw.comment_body,
        "comment_author": raw.comment_author,
        "comment_score": raw.comment_score,
        "comment_created_utc": raw.comment_created_utc,
        "keyword_match": raw.keyword_match,
        "signal_type": classification.get("signal_type", "noise"),
        "sentiment": classification.get("sentiment", "neutral"),
        "strength": classification.get("strength", "casual"),
        "confirmation_status": classification.get("confirmation_status", "self_announced"),
        "entities": json.dumps(classification.get("entities", [])),
        "summary": classification.get("summary", ""),
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }


def save_signals(
    rows: list[dict],
    topic: str,
    run_id: str | None = None,
) -> Path:
    """Write classified signals to a Parquet file. Returns the file path."""
    if not rows:
        log.warning("No rows to save")
        return SIGNALS_DIR / "empty.parquet"

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    path = SIGNALS_DIR / f"{topic}_{run_id}.parquet"
    table = pa.Table.from_pylist(rows, schema=_signals_schema())
    pq.write_table(table, path, compression="zstd")

    log.info("Saved %d signals to %s", len(rows), path)
    return path


def query_signals(
    topic: str | None = None,
    signal_type: str | None = None,
    sentiment: str | None = None,
    days: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query signals via DuckDB. Returns list of dicts."""
    con = duckdb.connect(":memory:")

    pattern = str(SIGNALS_DIR / "*.parquet")
    try:
        con.execute(f"CREATE VIEW signals AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
    except Exception:
        log.warning("No parquet files found in %s", SIGNALS_DIR)
        return []

    clauses = []
    params = {}
    if topic:
        clauses.append("topic = $topic")
        params["topic"] = topic
    if signal_type:
        clauses.append("signal_type = $signal_type")
        params["signal_type"] = signal_type
    if sentiment:
        clauses.append("sentiment = $sentiment")
        params["sentiment"] = sentiment
    if days:
        # classified_at is an ISO-8601 UTC string; same-format ISO strings
        # compare chronologically lexicographically, so bind the cutoff as a
        # plain param. (DuckDB can't bind a value inside INTERVAL literals.)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        clauses.append("classified_at >= $cutoff")
        params["cutoff"] = cutoff

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    query = f"""SELECT * FROM signals{where}
              ORDER BY post_score DESC, classified_at DESC
              LIMIT {limit}"""

    result = con.execute(query, params).fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()

    # Convert entities JSON string back to list
    rows = [dict(zip(columns, row)) for row in result]
    for r in rows:
        if isinstance(r.get("entities"), str):
            try:
                r["entities"] = json.loads(r["entities"])
            except json.JSONDecodeError:
                r["entities"] = []

    return rows


def get_signal_counts(topic: str) -> dict:
    """Get signal_type counts for a topic (for briefing context)."""
    con = duckdb.connect(":memory:")
    pattern = str(SIGNALS_DIR / f"{topic}_*.parquet")
    try:
        con.execute(f"CREATE VIEW signals AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
    except Exception:
        return {}

    result = con.execute(
        "SELECT signal_type, count(*) as cnt FROM signals GROUP BY signal_type ORDER BY cnt DESC"
    ).fetchall()
    con.close()
    return {row[0]: row[1] for row in result}


# ---------------------------------------------------------------------------
# Raw post store: data/raw/<topic>/<run_id>.parquet
# Persisted BEFORE any LLM call. Append-only, deduped on
# (source, post_id, comment_id), kept forever — enables crash-resume and
# delta classification.
# ---------------------------------------------------------------------------

_RAW_DEDUPE_KEY = "source, post_id, comment_id"


def _raw_row_dict(raw, run_id: str, topic: str) -> dict:
    return {
        "run_id": run_id,
        "topic": topic,
        "source": raw.source,
        "post_id": raw.post_id,
        "post_title": raw.post_title,
        "post_url": raw.post_url,
        "post_subreddit": raw.post_subreddit,
        "post_author": raw.post_author,
        "post_score": raw.post_score,
        "post_created_utc": raw.post_created_utc,
        "post_body": raw.post_body,
        "comment_id": raw.comment_id,
        "comment_body": raw.comment_body,
        "comment_author": raw.comment_author,
        "comment_score": raw.comment_score,
        "comment_created_utc": raw.comment_created_utc,
        "keyword_match": raw.keyword_match,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def load_raw_posts(topic: str) -> list[dict]:
    """Load all raw rows for a topic across its run snapshots."""
    con = duckdb.connect(":memory:")
    pattern = str(RAW_DIR / topic / "*.parquet")
    try:
        con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet('{pattern}')")
    except Exception:
        log.warning("No raw parquet files found for topic '%s' in %s", topic, RAW_DIR / topic)
        con.close()
        return []

    rows = [dict(zip([d[0] for d in con.description], r))
            for r in con.execute("SELECT * FROM raw").fetchall()]
    con.close()
    return rows


def save_raw_posts(
    raw_posts: list,
    topic: str,
    run_id: str,
) -> tuple[Path, int]:
    """Persist raw posts for this run, deduped against prior runs.

    Anti-join on (source, post_id, comment_id) against everything already in
    data/raw/<topic>/ — only genuinely new items land in this run's snapshot,
    so re-ingesting an overlapping window is idempotent at the raw layer.

    Returns (snapshot path, number of new rows written).
    """
    out_dir = RAW_DIR / topic
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.parquet"

    existing_keys: set[tuple] = set()
    for row in load_raw_posts(topic):
        existing_keys.add((row["source"], row["post_id"], row["comment_id"]))

    new_rows, skipped = [], 0
    for raw in raw_posts:
        key = (raw.source, raw.post_id, raw.comment_id)
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        new_rows.append(_raw_row_dict(raw, run_id, topic))

    table = pa.Table.from_pylist(new_rows, schema=_raw_schema())
    pq.write_table(table, path, compression="zstd")
    log.info("Raw store: %d new rows -> %s (%d duplicates skipped)",
             len(new_rows), path, skipped)
    return path, len(new_rows)


def pending_classifications(topic: str) -> list[dict]:
    """Delta for --classify-only: raw rows with no matching classified row.

    Anti-join on (source, post_id, comment_id). Returns raw-row dicts.
    Idempotent: after classification lands, the delta is empty.
    """
    con = duckdb.connect(":memory:")
    raw_pattern = str(RAW_DIR / topic / "*.parquet")
    sig_pattern = str(SIGNALS_DIR / f"{topic}_*.parquet")
    try:
        con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet('{raw_pattern}', union_by_name=true)")
    except Exception:
        log.warning("No raw rows for topic '%s' — nothing to classify", topic)
        con.close()
        return []
    try:
        con.execute(f"CREATE VIEW sig AS SELECT * FROM read_parquet('{sig_pattern}', union_by_name=true)")
    except Exception:
        log.info("No classified signals yet for topic '%s' — classifying full raw store", topic)
        con.execute("CREATE VIEW sig AS SELECT * FROM raw LIMIT 0")

    query = """
        SELECT r.* FROM raw r
        WHERE NOT EXISTS (
            SELECT 1 FROM sig s
            WHERE s.source IS NOT DISTINCT FROM r.source
              AND s.post_id IS NOT DISTINCT FROM r.post_id
              AND s.comment_id IS NOT DISTINCT FROM r.comment_id
        )
    """
    rows = [dict(zip([d[0] for d in con.description], r))
            for r in con.execute(query).fetchall()]
    con.close()
    return rows
