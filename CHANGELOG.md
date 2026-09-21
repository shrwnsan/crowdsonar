# Changelog

## v0.1.3

- **Auditability:** classified signals now carry `matched_phrase` provenance — the
  ingest-time keyword match survives into the signals parquet alongside the LLM
  verdict (nullable column; fully backward-compatible with pre-existing files via
  `union_by_name`, zero migration).
- **Community-rule awareness at ingest:** subreddit rules are fetched once per
  24h TTL and cached at `data/rules_cache/{subreddit}.json`. Best-effort — a
  failed prefetch never fails the run.
- **New `--seo` ranked-threads surface:** persistent per-topic ranked-threads
  store (`data/ranked_threads/{topic}.parquet`, upsert-deduped on `thread_id`),
  loaded via the `--seo-file` JSON escape hatch and appended to the digest with
  `--seo`.

Fully backward-compatible: no CLI breakage, no data migration required.

## v0.1.2

- 16384 synthesis budget + de-branded empty guard + TL;DR/legend + Action Items split.

## v0.1.1

- Raw post store + real `--classify-only` — delta classification.

## v0.1.0

- Initial release: ingest, classify, and synthesize community chatter into briefings.
