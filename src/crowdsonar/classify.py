"""LLM classification — batch posts through a cheap model, return structured signals."""

import json
import logging
import os
from importlib import resources

import httpx

log = logging.getLogger(__name__)

PROMPT_DIR = resources.files("crowdsonar") / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _build_batch_items(raw_posts) -> list[dict]:
    """Convert RawPost list into compact items for the LLM."""
    items = []
    for i, p in enumerate(raw_posts):
        text = p.post_title
        if p.comment_body:
            text += f"\n\nComment: {p.comment_body}"
        elif p.post_body:
            text += f"\n\n{p.post_body}"

        items.append({
            "idx": i,
            "text": text[:1500],  # truncate to save tokens
            "subreddit": p.post_subreddit,
            "keyword_match": p.keyword_match,
        })
    return items


def classify_batch(
    raw_posts: list,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    batch_size: int = 25,
) -> list[dict]:
    """Classify a list of RawPost objects via LLM.

    Returns a list of classification dicts, one per input post,
    aligned by index.
    """
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    if not api_key:
        raise ValueError("No API key provided. Set OPENAI_API_KEY or pass api_key.")

    system_prompt = _load_prompt("classify")
    all_classifications: list[dict] = []

    # Process in batches
    for start in range(0, len(raw_posts), batch_size):
        batch = raw_posts[start:start + batch_size]
        items = _build_batch_items(batch)

        user_msg = json.dumps(items, ensure_ascii=False)

        log.info("Classifying batch %d-%d (%d items) via %s",
                 start, start + len(batch), len(batch), model)

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
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
                timeout=120,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            # Strip markdown fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            parsed = json.loads(content)
            if not isinstance(parsed, list):
                parsed = [parsed]

            # Pad with defaults if LLM returned fewer items
            default = {
                "signal_type": "noise",
                "sentiment": "neutral",
                "strength": "casual",
                "entities": [],
                "summary": "",
            }
            while len(parsed) < len(batch):
                parsed.append({**default, "idx": len(parsed)})

            all_classifications.extend(parsed)

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            log.error("Failed to parse LLM response: %s", e)
            # Fill batch with noise defaults
            for i in range(len(batch)):
                all_classifications.append({
                    "idx": start + i,
                    "signal_type": "noise",
                    "sentiment": "neutral",
                    "strength": "casual",
                    "entities": [],
                    "summary": f"[classification failed: {e}]",
                })
        except Exception as e:
            log.error("LLM call failed: %s", e)
            for i in range(len(batch)):
                all_classifications.append({
                    "idx": start + i,
                    "signal_type": "noise",
                    "sentiment": "neutral",
                    "strength": "casual",
                    "entities": [],
                    "summary": f"[classification failed: {e}]",
                })

    # Sort by idx to align with input
    all_classifications.sort(key=lambda x: x.get("idx", 0))
    return all_classifications
