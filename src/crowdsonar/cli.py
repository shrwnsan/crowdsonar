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
from crowdsonar.storage import save_signals, query_signals
from crowdsonar.synthesize import synthesize_briefing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("crowdsonar")


def parse_args():
    p = argparse.ArgumentParser(description="CrowdSonar — social signal synthesis engine")
    p.add_argument("--topic", "-t", help="Topic config name (from configs/)")
    p.add_argument("--list", "-l", action="store_true", help="List available topics")
    p.add_argument("--dry-run", action="store_true", help="Ingest only, skip classification and synthesis")
    p.add_argument("--source", choices=["auto", "arctic", "praw"], default="auto",
                   help="Ingestion source: arctic = Arctic Shift API (credential-free, "
                        "default); praw = Reddit OAuth only; auto = arctic + PRAW overlay "
                        "when REDDIT_CLIENT_ID/SECRET are set")
    p.add_argument("--backfill", action="store_true",
                   help="Widen the ingest window to backfill_days from the topic config "
                        "(one-off deep backfill; default window is time_filter)")
    p.add_argument("--classify-only", action="store_true", help="Only run classification on stored data")
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

        if args.dry_run or not all_posts:
            print(f"\n[DRY RUN] Would process {len(all_posts)} items from {len(reddit_cfg['subreddits'])} subreddits")
            for p in all_posts[:5]:
                label = "comment" if p.comment_id else "post"
                print(f"  [{label}] r/{p.post_subreddit} ({p.keyword_match}) score={p.post_score} — {p.post_title[:80]}")
            if len(all_posts) > 5:
                print(f"  ... and {len(all_posts) - 5} more")
            return

    # ── Phase 2: Classify ──
    if not args.synthesize_only:
        if args.classify_only:
            # Load from last parquet — but we need raw posts for classification
            # For classify-only, we'd need raw data stored separately
            log.error("--classify-only requires stored raw posts (not yet implemented)")
            log.error("Run a full pipeline first, or use --dry-run to inspect ingestion.")
            sys.exit(1)

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

    # ── Phase 3: Synthesize ──
    if not args.dry_run:
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


if __name__ == "__main__":
    main()
