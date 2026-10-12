"""crowdsonar doctor — health matrix for every backend the pipeline touches.

One command that answers "can a run actually work right now?": public-archive
reachability, credential presence, LLM endpoint auth, and local store
writability. Designed for humans AND cron: prints one line per backend and
exits 1 when a required check fails, 0 otherwise.

Checks are single-attempt and short-timeout on purpose — a doctor that retries
for a minute before reporting is worse than one that fails fast.
"""

import os
import time
from dataclasses import dataclass

import httpx

from . import __version__, storage
from .arctic_shift import BASE_URL

ARCTIC_PROBE_PARAMS = {"subreddit": "askreddit", "limit": 1, "fields": "id"}


@dataclass
class CheckResult:
    name: str
    status: str   # "ok" | "warn" | "fail"
    detail: str


def check_arctic(base_url: str = BASE_URL, timeout: float = 15.0) -> CheckResult:
    """One tiny query against the public archive — reachability + latency."""
    started = time.monotonic()
    try:
        resp = httpx.get(f"{base_url}/api/posts/search", params=ARCTIC_PROBE_PARAMS,
                         timeout=timeout)
    except httpx.HTTPError as e:
        return CheckResult("arctic", "fail", f"unreachable ({e.__class__.__name__})")

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code == 429:
        reset = resp.headers.get("X-RateLimit-Reset") or "?"
        return CheckResult("arctic", "fail", f"rate limited (reset in {reset}s)")
    if resp.status_code != 200:
        return CheckResult("arctic", "fail", f"HTTP {resp.status_code}")

    body = resp.json()
    if body.get("error"):
        return CheckResult("arctic", "fail", f"API error: {body['error']}")

    n = len(body.get("data") or [])
    remaining = resp.headers.get("X-RateLimit-Remaining")
    detail = f"reachable in {latency_ms}ms ({n} post sampled"
    if remaining:
        detail += f", quota remaining {remaining}"
    return CheckResult("arctic", "ok", detail + ")")


def check_reddit_creds() -> CheckResult:
    """Credential presence for the optional real-time overlay (never fails the run)."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not (client_id and client_secret):
        return CheckResult("reddit", "warn",
                           "REDDIT_CLIENT_ID/SECRET not set — real-time overlay disabled")

    # Deferred import: only operators with credentials pay the praw import cost.
    from .ingest import create_praw_client, ingest_praw

    try:
        reddit = create_praw_client(
            client_id=client_id, client_secret=client_secret,
            user_agent=f"crowdsonar/{__version__} (health check)",
        )
        result = ingest_praw(reddit, subreddits=["askreddit"], keywords=[],
                             post_limit=1, comments_per_post=0)
    except Exception as e:
        return CheckResult("reddit", "warn", f"credentials set but probe failed ({e.__class__.__name__})")

    if result.errors:
        return CheckResult("reddit", "warn", f"credentials set but listing failed: {result.errors[0]}")
    return CheckResult("reddit", "ok", "credentials valid — overlay ready")


def check_llm() -> CheckResult:
    """API key presence + endpoint auth via a models listing."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        return CheckResult("llm", "warn",
                           "OPENAI_API_KEY not set — classify/synthesize will fail until it is")

    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/models",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0)
    except httpx.HTTPError as e:
        return CheckResult("llm", "fail", f"endpoint unreachable ({e.__class__.__name__})")

    if resp.status_code in (401, 403):
        return CheckResult("llm", "fail", f"endpoint rejected the key (HTTP {resp.status_code})")
    if resp.status_code != 200:
        return CheckResult("llm", "fail", f"HTTP {resp.status_code} from {base_url}")

    models = resp.json().get("data") or []
    return CheckResult("llm", "ok", f"endpoint authenticated ({len(models)} models visible)")


def check_storage() -> CheckResult:
    """DATA_DIR writable + a census of what's already banked.

    Paths are read off the storage module at call time (not imported at module
    scope) so redirected data dirs — tests, CROWDSONAR_DATA_DIR — are honored.
    """
    data_dir, signals_dir, raw_dir = storage.DATA_DIR, storage.SIGNALS_DIR, storage.RAW_DIR
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor_write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return CheckResult("storage", "fail", f"{data_dir} not writable ({e.__class__.__name__})")

    signals = len(list(signals_dir.glob("*.parquet"))) if signals_dir.exists() else 0
    topics = len([d for d in raw_dir.iterdir() if d.is_dir()]) if raw_dir.exists() else 0
    return CheckResult("storage", "ok",
                       f"{data_dir} writable ({signals} signal file(s), {topics} raw topic(s))")


def run_doctor() -> int:
    """Run all checks, print the matrix, return the process exit code.

    Each check is crash-guarded: the doctor's own contract is "always print a
    matrix, always exit cleanly" — a non-JSON 200 from a captive portal is a
    finding, not a traceback.
    """
    checks = [
        ("arctic", check_arctic),
        ("reddit", check_reddit_creds),
        ("llm", check_llm),
        ("storage", check_storage),
    ]
    results = []
    for name, fn in checks:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001 — the guard IS the feature
            results.append(CheckResult(name, "fail", f"check crashed: {e.__class__.__name__}"))

    print(f"crowdsonar doctor — v{__version__}")
    glyph = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    for r in results:
        print(f"  {glyph[r.status]}  {r.name:<8} {r.detail}")

    failed = [r.name for r in results if r.status == "fail"]
    if failed:
        print(f"\n{len(failed)} required check(s) failed: {', '.join(failed)}")
        return 1
    warned = [r.name for r in results if r.status == "warn"]
    if warned:
        print(f"\nAll required checks passed ({', '.join(warned)} need attention).")
    else:
        print("\nAll checks passed.")
    return 0
