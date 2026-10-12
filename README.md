# CrowdSonar

Self-hosted social signal synthesis engine. Ingest posts and comments from social platforms (Reddit first), classify them by signal type, sentiment, and strength, cluster themes, and synthesize actionable intelligence into briefings.

Point it at any community conversation with a YAML topic config — subreddits, keywords, taxonomy — and get a decision-ready briefing instead of a raw firehose.

## How It Works

```
TOPIC CONFIG ──▶ INGEST ──▶ CLASSIFY ──▶ SYNTHESIZE ──▶ DELIVER
  (YAML)         Arctic      via any      rank +        terminal
                 Shift API   OpenAI-      cluster       briefing
                             compatible
                             endpoint
```

## CLI Usage

```
crowdsonar --topic <name> [flags]
crowdsonar doctor
```

| Command / flag | What it does |
|------|--------------|
| `doctor` | Health-check every backend (archive reachability, credentials, LLM endpoint, local store) — exit 1 if a required check fails |
| `--topic`, `-t` | Topic config to run (from `src/crowdsonar/configs/` or `--config-dir`) |
| `--list`, `-l` | List available topics |
| *(default run)* | Full pipeline: ingest → classify → synthesize |
| `--dry-run` | Ingest only — skip classification and synthesis |
| `--source` | `auto` (default) = freshness-aware router — archive first, PRAW overlay only when the archive lacks coverage of the freshness window; `arctic` = archive only; `praw` = Reddit OAuth only |
| `--overlay` | With `--source auto`: `router` (default) gates the overlay on archive coverage; `always` = legacy composition (overlay whenever credentials are set) |
| `--backfill` | One-off deep backfill — widens the ingest window to `backfill_days` from the topic config |
| `--classify-only` | Only classify already-stored data |
| `--synthesize-only` | Only generate a briefing from classified data |
| `--model` | Classification model (default: `gpt-4o-mini`) |
| `--synth-model` | Synthesis model (default: same as `--model`) |
| `--config-dir` | Override the configs directory |

## Quick Start

Requires Python 3.10+ (developed on 3.13).

```bash
# 1. Create a venv and install
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. Configure credentials (env vars or .env)
export OPENAI_API_KEY="..."        # required — LLM access for classify + synthesize
export OPENAI_BASE_URL="..."       # optional — any OpenAI-compatible endpoint
export REDDIT_CLIENT_ID="..."      # optional — enables real-time PRAW ingest
export REDDIT_CLIENT_SECRET="..."  # optional

# 3. Run
crowdsonar --list
crowdsonar --topic example
```

Default `--source auto` is a **freshness-aware router**: the Arctic Shift API (credential-free) runs first, then one keyword-free coverage probe per subreddit asks whether the archive holds anything newer than the topic's `freshness_hours` (default 48h). The PRAW real-time overlay runs only for subreddits the archive doesn't reach — archive lag, not topic quietness, is what fires it. When the archive effectively failed the pass, the run says so at WARNING instead of exiting zero silently. Fresh archive + no credentials is a fully functional zero-key run. Decision table: `docs/ingest.md`.

Any OpenAI-compatible chat-completions endpoint works for classification and synthesis — point `OPENAI_BASE_URL` at Mistral, DeepSeek, OpenRouter, a local llama.cpp server, anything speaking the OpenAI protocol.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENAI_API_KEY` | yes | — | LLM API key (classification + synthesis) |
| `OPENAI_BASE_URL` | no | `https://api.openai.com/v1` | Any OpenAI-compatible chat-completions endpoint |
| `REDDIT_CLIENT_ID` | no | — | Reddit OAuth app ID — enables real-time PRAW ingest |
| `REDDIT_CLIENT_SECRET` | no | — | Reddit OAuth app secret |

Data is written to `./data/` relative to where you run the command; override with `CROWDSONAR_DATA_DIR`.

## Dependencies

Managed via `pyproject.toml` (`pip install -e .`):

| Package | Role |
|---------|------|
| `praw` | Reddit API ingest (optional overlay) |
| `duckdb` | Signal store queries |
| `pyarrow` | Parquet storage |
| `httpx` | Arctic Shift / LLM HTTP client |
| `pyyaml` | Topic config loading |

## Storage

DuckDB + Parquet. One `signals.parquet` per topic/run; DuckDB queries across them. Analytical, not transactional.

## Provenance & Health

Every row carries the backend it came from (`arctic` / `praw`), and every
briefing ends with a deterministic ingest-mix line — e.g.
`arctic (public archive) 94% · praw (real-time) 6%` — computed from the stored
rows after synthesis, not by the model.

`crowdsonar doctor` checks the full backend matrix before you depend on it:
archive reachability + latency, Reddit credential validity, LLM endpoint auth,
and local store writability. Exit code 1 on any required failure, so schedules
can gate on it. Details: [docs/ingest.md](docs/ingest.md).

## Adding a Topic

Point `--config-dir` at a directory of YAML files (or drop one into `src/crowdsonar/configs/`), then change the name, subreddits, keywords, and taxonomy. No code changes — the pipeline is config-driven.

## Project Layout

```
src/crowdsonar/
  cli.py             # console entry point (ingest → classify → synthesize, doctor)
  config.py          # topic YAML loader (importlib.resources; --config-dir override)
  ingest.py          # PRAW ingestion (optional overlay)
  arctic_shift.py    # Arctic Shift API adapter (default source)
  classify.py        # LLM classification (signal type, sentiment, strength)
  storage.py         # DuckDB + Parquet signal store
  synthesize.py      # briefing generation + ingest-mix provenance line
  doctor.py          # backend health matrix (`crowdsonar doctor`)
  configs/           # bundled topic configs (example.yaml)
  prompts/           # classifier + synthesizer prompt templates (packaged data)
docs/
  ingest.md          # source tiers, health checks, provenance
tests/               # contract tests (zero network)
```

## Status

Alpha — the full ingest → classify → synthesize pipeline runs end-to-end via Arctic Shift (credential-free). Roadmap: cron briefing delivery, threshold alerts, additional sources (HN, Bluesky, RSS).

## License

MIT — see [LICENSE](LICENSE).
