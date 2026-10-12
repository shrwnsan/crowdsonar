"""LLM synthesis — generate a briefing from classified signals."""

import json
import logging
import os
from importlib import resources

import httpx

from .storage import get_signal_counts, ingest_mix, query_signals

log = logging.getLogger(__name__)

PROMPT_DIR = resources.files("crowdsonar") / "prompts"

# Provenance labels for the ingest-mix line appended to every briefing.
SOURCE_LABELS = {
    "arctic": "arctic (public archive)",
    "praw": "praw (real-time)",
}


def _mix_percentages(mix: dict) -> list[tuple[str, int]]:
    """Source → integer percent via largest remainder (sums to exactly 100)."""
    total = sum(mix.values())
    if not total:
        return []
    exact = {s: 100 * n / total for s, n in mix.items()}
    floors = {s: int(v) for s, v in exact.items()}
    leftover = 100 - sum(floors.values())
    # Hand leftover points to the largest fractional parts (ties → bigger source).
    order = sorted(exact, key=lambda s: (exact[s] % 1, floors[s]), reverse=True)
    for s in order[:leftover]:
        floors[s] += 1
    return sorted(floors.items(), key=lambda sv: -sv[1])


def _provenance_footer(mix: dict, time_label: str) -> str:
    """Deterministic ingest-mix line, e.g. `arctic (public archive) 94% · praw (real-time) 6%`.

    Computed from the STORE (all classified rows in the window via
    storage.ingest_mix), never from the LLM-facing slice, and appended after
    the LLM call — the mix cannot be hallucinated. No `---` separator: the
    line must not collide with frontmatter/section-splitting parsers.
    """
    if not mix:
        return ""
    total = sum(mix.values())
    parts = [
        f"{SOURCE_LABELS.get(source, source)} {pct}%"
        for source, pct in _mix_percentages(mix)
    ]
    return f"*Ingest mix ({time_label}): {' · '.join(parts)} — n={total} rows classified*"


def synthesize_briefing(
    topic: str,
 topic_cfg: dict,
    run_id: str | None = None,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    days: int = 7,
) -> str:
    """Generate a text briefing from stored classified signals."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    if not api_key:
        raise ValueError("No API key. Set OPENAI_API_KEY or pass api_key.")

    # Fetch signals
    signals = query_signals(topic=topic, days=days, limit=200)
    if not signals:
        return f"No signals found for topic '{topic}' in the past {days} days."

    # Filter out noise for synthesis
    actionable = [s for s in signals if s.get("signal_type") != "noise"]
    if not actionable:
        return f"All {len(signals)} signals for '{topic}' were classified as noise."

    signal_counts = get_signal_counts(topic)
    time_label = topic_cfg.get("synthesis", {}).get("time_range_label", f"past {days} days")

    # Provenance: store-wide counts per backend for the whole window — not the
    # score-ranked slice below — so the footer describes everything banked.
    mix = ingest_mix(topic, days=days)

    # Build signal summaries for the LLM
    signal_items = []
    for s in actionable:
        signal_items.append({
            "signal_type": s["signal_type"],
            "sentiment": s["sentiment"],
            "strength": s["strength"],
            "confirmation_status": s.get("confirmation_status", "self_announced"),
            "entities": s["entities"],
            "summary": s["summary"],
            "subreddit": s["post_subreddit"],
            "score": s["post_score"],
            "url": s["post_url"],
        })

    # Truncate to top N
    top_n = topic_cfg.get("synthesis", {}).get("top_signals", 10)
    signal_items = signal_items[:top_n]

    system_prompt = (PROMPT_DIR / "synthesize.md").read_text(encoding="utf-8")

    user_msg = json.dumps({
        "topic": topic_cfg["name"],
        "topic_description": topic_cfg.get("description", ""),
        "signals": signal_items,
        "signal_counts": signal_counts,
        "ingest_mix": mix,
        "time_range": time_label,
    }, ensure_ascii=False)

    log.info("Synthesizing briefing (%d signals) via %s", len(signal_items), model)

    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.3,
                # Some OpenAI-compatible backends spend reasoning tokens
                # inside max_tokens — budget generously or content starves.
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
                "max_tokens": 16384,
            },
            timeout=120,
        )
        resp.raise_for_status()
        briefing = resp.json()["choices"][0]["message"]["content"].strip()
        if not briefing:
            # Empty content = backend produced no visible output (e.g. reasoning
            # tokens consumed the token budget). Never return "" — a silent
            # empty briefing gets persisted as a 0-byte file and renders as a
            # body-less digest.
            return "⚠️ Synthesis returned empty content — raise the token budget or lower reasoning_effort."
        # Append the deterministic provenance line AFTER the LLM output so the
        # mix can't be hallucinated — it always reflects the stored rows.
        footer = _provenance_footer(mix, time_label)
        return f"{briefing}\n\n{footer}" if footer else briefing

    except Exception as e:
        log.error("Synthesis failed: %s", e)
        return f"Synthesis failed: {e}"
