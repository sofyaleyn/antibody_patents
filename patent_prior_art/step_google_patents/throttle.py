"""Client-side token-per-minute throttle + 429 retry helper."""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from functools import wraps

import anthropic

log = logging.getLogger(__name__)

# Anthropic free/low-tier default for sonnet-4-6 is 30K ITPM.
# Leave headroom — cache reads still count partially toward the limit.
DEFAULT_ITPM = 28_000

_window: deque[tuple[float, int]] = deque()  # (timestamp, tokens)


def _purge(now: float) -> None:
    while _window and now - _window[0][0] >= 60.0:
        _window.popleft()


def reserve(tokens: int, itpm: int = DEFAULT_ITPM) -> None:
    """Block until sending `tokens` more would stay under itpm in a 60s window."""
    while True:
        now = time.monotonic()
        _purge(now)
        used = sum(t for _, t in _window)
        if used + tokens <= itpm:
            _window.append((now, tokens))
            return
        # Sleep until the oldest entry ages out
        wait = 60.0 - (now - _window[0][0]) + 0.5
        log.info(f"Throttle: {used:,} ITPM used + {tokens:,} pending; sleeping {wait:.1f}s")
        time.sleep(max(wait, 1.0))


def _retry_after_seconds(err: anthropic.APIError) -> float:
    msg = str(err)
    m = re.search(r"retry[- ]after[\"']?\s*[:=]\s*[\"']?(\d+)", msg, re.I)
    if m:
        return float(m.group(1))
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    for k in ("retry-after", "anthropic-ratelimit-input-tokens-reset"):
        v = headers.get(k) if hasattr(headers, "get") else None
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return 30.0


def call_with_throttle(
    client: anthropic.Anthropic,
    *,
    estimated_input_tokens: int,
    itpm: int = DEFAULT_ITPM,
    max_retries: int = 6,
    **kwargs,
):
    """Wrap client.messages.create with ITPM reservation and 429 backoff."""
    for attempt in range(1, max_retries + 1):
        reserve(estimated_input_tokens, itpm=itpm)
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = _retry_after_seconds(e)
            log.warning(
                f"429 rate-limited (attempt {attempt}/{max_retries}); sleeping {wait:.1f}s"
            )
            time.sleep(wait)
        except anthropic.APIError as e:
            if attempt == max_retries:
                raise
            log.error(f"API error (attempt {attempt}/{max_retries}): {e}")
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("call_with_throttle exhausted retries")
