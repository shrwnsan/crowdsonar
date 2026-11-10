"""LLM synthesis — generate a briefing from classified signals."""

import json
import logging
import os
from importlib import resources

import httpx

from .storage import get_signal_counts, query_signals

log = logging.getLogger(__name__)

PROMPT_DIR = resources.files("crowdsonar") / "prompts"


class SynthesisError(RuntimeError):
    """LLM synthesis failed — no briefing was produced (and none is persisted)."""


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

    # Deterministic counts header — pure arithmetic over the stored signal
    # window, NO model attribution in it.
    counts_header = (
        f"{len(signals)} classified → "
        f"{len(signals) - len(actionable)} noise filtered → "
        f"{len(signal_items)} shown"
    )

    system_prompt = (PROMPT_DIR / "synthesize.md").read_text(encoding="utf-8")

    user_msg = json.dumps({
        "topic": topic_cfg["name"],
        "topic_description": topic_cfg.get("description", ""),
        "signals": signal_items,
        "signal_counts": signal_counts,
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
                "max_tokens": 8192,
            },
            # 120s was exceeded by legitimate high-effort reasoning once —
            # the read timed out and the failure string briefly shipped as a
            # briefing. 300s covers slow reasoning turns with headroom.
            timeout=300,
        )
        resp.raise_for_status()
        briefing = resp.json()["choices"][0]["message"]["content"].strip()
        if not briefing:
            # Empty content with HTTP 200 = reasoning-token starvation
            # (reasoning consumed max_tokens before any content was emitted).
            # Failure is never content: raise instead of returning failure
            # text — a returned string got persisted as a one-line poison
# briefing file. Callers surface the error.
            raise SynthesisError(
                "synthesis returned empty content — reasoning-token starvation; "
                "raise max_tokens or lower reasoning_effort"
            )
        # Counts header rides on top of the LLM prose — deterministic prefix,
        # model-free, so the brief opens with the funnel numbers.
        return f"{counts_header}\n\n{briefing}"

    except SynthesisError:
        raise
    except Exception as e:
        log.error("Synthesis failed: %s", e)
        raise SynthesisError(str(e)) from e
