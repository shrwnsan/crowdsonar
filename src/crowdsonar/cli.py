#!/usr/bin/env python3
"""CrowdSonar CLI — ingest, classify, synthesize social signals.

Usage:
    crowdsonar --topic example
    crowdsonar --topic example --dry-run
    crowdsonar --topic example --source arctic
    crowdsonar --topic example --backfill
    crowdsonar --topic example --classify-only
    crowdsonar --topic example --synthesize-only
    crowdsonar --list
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from crowdsonar import __version__
from crowdsonar.config import load_topic, list_topics
from crowdsonar.ingest import (
    IngestResult,
    RawPost,
    ingest_praw,
    create_praw_client,
)
from crowdsonar.arctic_shift import ingest_arctic
from crowdsonar.classify import classify_batch
from crowdsonar.storage import (
    save_signals,
    save_raw_posts,
    pending_classifications,
)
from crowdsonar.synthesize import synthesize_briefing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("crowdsonar")


def _raw_row_to_post(row: dict) -> RawPost:
    """Convert a raw-store row dict back into a RawPost for classification."""
    return RawPost(
        source=row["source"],
        post_id=row["post_id"],
        post_title=row["post_title"],
        post_url=row["post_url"],
        post_subreddit=row["post_subreddit"],
        post_author=row["post_author"],
        post_score=row["post_score"],
        post_created_utc=row["post_created_utc"],
        post_body=row["post_body"],
        comment_id=row["comment_id"],
        comment_body=row["comment_body"],
        comment_author=row["comment_author"],
        comment_score=row["comment_score"],
        comment_created_utc=row["comment_created_utc"],
        keyword_match=row["keyword_match"],
    )


def parse_args():
    p = argparse.ArgumentParser(description="CrowdSonar — social signal synthesis engine")
    p.add_argument("--topic", "-t", help="Topic config name (from configs/)")
    p.add_argument("--list", "-l", action="store_true", help="List available topics")
    p.add_argument("--dry-run", action="store_true", help="Ingest and persist the raw store, skip all LLM calls")
    p.add_argument("--source", choices=["auto", "arctic", "praw"], default="auto",
                   help="Ingestion source: arctic = Arctic Shift API (credential-free, "
                        "default); praw = Reddit OAuth only; auto = arctic + PRAW overlay "
                        "when REDDIT_CLIENT_ID/SECRET are set")
    p.add_argument("--backfill", action="store_true",
                   help="Widen the ingest window to backfill_days from the topic config "
                        "(one-off deep backfill; default window is time_filter)")
    p.add_argument("--classify-only", action="store_true", help="Classify the pending raw/classified delta via LLM, append to signals (idempotent)")
    p.add_argument("--synthesize-only", action="store_true", help="Only generate briefing from classified data")
    p.add_argument("--model", default="gpt-4o-mini", help="LLM model for classification")
    p.add_argument("--synth-model", default=None, help="LLM model for synthesis (default: same as --model)")
    p.add_argument("--config-dir", default=None, help="Override configs directory")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list:
        topics = list_topics(args.config_dir)
        if not topics:
            print("No topic configs found in configs/")
        else:
            print("Available topics:")
            for t in topics:
                print(f"  - {t}")
        return

    if not args.topic:
        print("Error: --topic is required (or use --list to see available topics)")
        sys.exit(1)

    # Load config
    cfg = load_topic(args.topic, args.config_dir)
    topic_name = cfg["name"]
    reddit_cfg = cfg["sources"]["reddit"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    log.info("Topic: %s | Run: %s | Source: %s", topic_name, run_id, args.source)

    # ── Phase 1: Ingest ──
    all_posts: list[RawPost] = []
    pending: list[dict] = []  # raw/classified delta; computed in Phase 1 or --classify-only

    if not args.synthesize_only and not args.classify_only:
        backfill_days = reddit_cfg.get("backfill_days", 0) if args.backfill else 0

        # Arctic Shift — primary, credential-free
        if args.source in ("auto", "arctic"):
            label = f"window={backfill_days}d (backfill)" if backfill_days else \
                    f"time_filter={reddit_cfg.get('time_filter', 'week')}"
            log.info("Phase 1a: Arctic Shift ingestion (%s)", label)
            arctic_result = ingest_arctic(
                subreddits=reddit_cfg["subreddits"],
                keywords=reddit_cfg["keywords"],
                time_filter=reddit_cfg.get("time_filter", "week"),
                post_limit=reddit_cfg.get("post_limit", 50),
                comments_per_post=reddit_cfg.get("comments_per_post", 20),
                backfill_days=backfill_days,
            )
            all_posts.extend(arctic_result.posts)
            for e in arctic_result.errors:
                log.warning("Arctic Shift: %s", e)

        # PRAW — optional real-time overlay
        if args.source in ("auto", "praw"):
            client_id = os.environ.get("REDDIT_CLIENT_ID")
            client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
            if client_id and client_secret:
                log.info("Phase 1b: PRAW real-time ingestion (overlay)")
                reddit = create_praw_client(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_agent=f"crowdsonar/{__version__} (research project)",
                )
                praw_result = ingest_praw(
                    reddit=reddit,
                    subreddits=reddit_cfg["subreddits"],
                    keywords=reddit_cfg["keywords"],
                    sort=reddit_cfg.get("sort", "new"),
                    time_filter=reddit_cfg.get("time_filter", "week"),
                    post_limit=reddit_cfg.get("post_limit", 50),
                    comments_per_post=reddit_cfg.get("comments_per_post", 20),
                )
                all_posts.extend(praw_result.posts)
                for e in praw_result.errors:
                    log.warning("PRAW: %s", e)
            elif args.source == "praw":
                log.error("--source praw requires REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET")
                sys.exit(1)
            else:
                log.info("No REDDIT_CLIENT_ID/SECRET — PRAW overlay skipped (Arctic Shift only)")

        # Deduplicate by (post_id, comment_id)
        seen = set()
        deduped = []
        for p in all_posts:
            key = (p.post_id, p.comment_id)
            if key not in seen:
                seen.add(key)
                deduped.append(p)
        all_posts = deduped

        log.info("Ingest complete: %d unique items", len(all_posts))

        # Persist raw BEFORE any LLM call (raw store) —
        # banks the fetch even when we stop here (dry-run / empty window).
        raw_path, new_raw = save_raw_posts(all_posts, topic_name, run_id)
        log.info("Raw store: %d new rows -> %s", new_raw, raw_path)

        # Classification operates on the raw/classified delta — idempotent
        # across re-ingest and crash-resume (raw persists pre-LLM).
        pending = pending_classifications(topic_name)

        if args.dry_run:
            print(f"\n[DRY RUN] {len(all_posts)} items ingested from {len(reddit_cfg['subreddits'])} subreddits; "
                  f"{new_raw} new raw rows persisted to {raw_path}.")
            print(f"Pending classification delta: {len(pending)} item(s). No LLM calls made.")
            for p in all_posts[:5]:
                label = "comment" if p.comment_id else "post"
                print(f"  [{label}] r/{p.post_subreddit} ({p.keyword_match}) score={p.post_score} — {p.post_title[:80]}")
            if len(all_posts) > 5:
                print(f"  ... and {len(all_posts) - 5} more")
            return

        if not all_posts:
            print(f"\nNo new items ingested this run (raw store: {raw_path}); "
                  f"{len(pending)} pending item(s) in the classification delta.")
            # fall through to Phase 2 — the delta may hold crash-resume work

    # ── Phase 2: Classify ──
    if not args.synthesize_only:
        if args.classify_only:
            # Skip ingest; operate purely on the raw/classified delta.
            pending = pending_classifications(topic_name)
            if not pending:
                print(f"\n--classify-only: 0 pending — raw store for '{topic_name}' is fully classified.")
                return
            print(f"--classify-only: {len(pending)} pending item(s) in the raw/classified delta")

        all_posts = [_raw_row_to_post(r) for r in pending]

        if all_posts:
            log.info("Phase 2: Classifying %d items via %s", len(all_posts), args.model)
            classifications = classify_batch(all_posts, model=args.model)

            # Save to Parquet
            from crowdsonar.storage import _row_dict
            rows = []
            for raw, cls in zip(all_posts, classifications):
                rows.append(_row_dict(raw, cls, run_id, topic_name))

            path = save_signals(rows, topic_name, run_id)
            print(f"\nSaved {len(rows)} classified signals to {path}")

            # Quick summary
            type_counts = {}
            for r in rows:
                t = r["signal_type"]
                type_counts[t] = type_counts.get(t, 0) + 1
            print("Signal breakdown:")
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
                print(f"  {t}: {c}")
        else:
            log.info("Phase 2: classification delta is empty — nothing to classify")

    # ── Phase 3: Synthesize ──
    if not args.dry_run and not args.classify_only:
        synth_model = args.synth_model or args.model
        log.info("Phase 3: Synthesizing briefing via %s", synth_model)
        briefing = synthesize_briefing(
            topic=topic_name,
            topic_cfg=cfg,
            run_id=run_id,
            model=synth_model,
        )
        print(f"\n{'='*60}")
        print(f"BRIEFING: {topic_name}")
        print(f"{'='*60}")
        print(briefing)

        # Persist briefing to Zone 1
        from crowdsonar.storage import DATA_DIR
        briefings_dir = DATA_DIR / "briefings"
        briefings_dir.mkdir(parents=True, exist_ok=True)
        briefing_path = briefings_dir / f"{topic_name}_{run_id}.md"
        briefing_path.write_text(briefing, encoding="utf-8")
        log.info("Briefing saved to %s", briefing_path)


if __name__ == "__main__":
    main()
