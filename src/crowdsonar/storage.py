"""DuckDB + Parquet storage for classified signals."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

# Data lives relative to the working directory (never inside site-packages
# once installed); override with CROWDSONAR_DATA_DIR.
DATA_DIR = Path(os.environ.get("CROWDSONAR_DATA_DIR", Path.cwd() / "data"))
SIGNALS_DIR = DATA_DIR / "signals"
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
        ("entities", pa.string()),       # JSON list
        ("summary", pa.string()),        # one-line LLM summary
        ("classified_at", pa.string()),  # ISO timestamp
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
        con.execute(f"CREATE VIEW signals AS SELECT * FROM read_parquet('{pattern}')")
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
        clauses.append("classified_at >= (now() - interval '$days days')::varchar")

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
        con.execute(f"CREATE VIEW signals AS SELECT * FROM read_parquet('{pattern}')")
    except Exception:
        return {}

    result = con.execute(
        "SELECT signal_type, count(*) as cnt FROM signals GROUP BY signal_type ORDER BY cnt DESC"
    ).fetchall()
    con.close()
    return {row[0]: row[1] for row in result}
