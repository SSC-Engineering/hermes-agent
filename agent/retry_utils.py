"""Retry utilities — jittered backoff for decorrelated retries.

Replaces fixed exponential backoff with jittered delays to prevent
thundering-herd retry spikes when multiple sessions hit the same
rate-limited provider concurrently.
"""

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan's GLM-5.2 endpoint often returns HTTP 429 code 1305
# ("The service may be temporarily overloaded...") for otherwise valid
# Hermes requests. Short retries tend to hammer the same overloaded window;
# after a few normal retries, progressively widen the wait window. Keep the
# cap interactive-friendly: a simple TUI message should fail visibly in minutes,
# not sit silent for 20+ minutes.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)

# Number of initial short retries before the adaptive long-backoff tier kicks
# in. Shared by ``adaptive_rate_limit_backoff`` (which walks the long table
# starting at attempt ``short_attempts + 1``) and
# ``zai_coding_overload_retry_ceiling`` (which sizes the retry loop so every
# long-tier entry is reachable). Keeping it a single module constant prevents
# the two from silently desyncing if the short-retry count is ever tuned.
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = 3


def parse_retry_after_seconds(value_or_headers: Any) -> Optional[float]:
    """Parse a ``Retry-After`` value into non-negative seconds.

    Accepts either a raw header value (numeric string / HTTP-date / number)
    or a headers mapping, in which case the ``Retry-After`` key is looked up
    case-insensitively (``.get`` on dict-like objects tries both common
    casings; real HTTP header containers like httpx/requests are already
    case-insensitive).

    Returns:
        Seconds as a ``float`` (negative deltas clamped to ``0.0``), or
        ``None`` when the header is absent or unparseable.
    """
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        # Looks like a headers mapping — pull the header out of it.
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                value = getter("Retry-After")
                if value is None:
                    value = getter("retry-after")
            except Exception:
                return None
            raw = value
        else:
            return None
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range.  0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
        getattr(error, "response", None),
    ]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """Return True for Z.AI Coding Plan transient overload 429s.

    The coding-plan endpoint reports overload as HTTP 429 with body code 1305
    and message "The service may be temporarily overloaded...". Treat only
    that narrow shape specially so ordinary quota/billing 429s still fail fast
    through the existing classifier.
    """
    base = (base_url or "").lower()
    model_name = (model or "").lower()
    status = getattr(error, "status_code", None)
    text = _error_text(error)
    return (
        status == 429
        and "api.z.ai/api/coding/paas/v4" in base
        and "glm-5.2" in model_name
        and ("1305" in text or "temporarily overloaded" in text)
    )


# Free NVIDIA NIM (integrate.api.nvidia.com) rate-limits intermittently —
# live probes 2026-08-25 saw ~1/3 of Ultra calls return 429 then recover.
# Without a retry layer, every free-tier 429 fell straight through to the
# paid fallback_model rail (nous/grok). Bounded attempts so a genuinely
# exhausted quota still fails loudly rather than hanging. (HEL-6108)
_NVIDIA_NIM_HOST_MARKERS = (
    "integrate.api.nvidia.com",
    "api.nvcf.nvidia.com",
)
# Default free-tier retry budget before paid fallback is considered.
# Tuned for interactive agent loops: short enough to fail visibly,
# long enough to ride out the intermittent free-tier windows.
_NVIDIA_NIM_DEFAULT_RETRY_ATTEMPTS = 3


def is_nvidia_nim_endpoint(
    *,
    provider: str | None = None,
    base_url: str | None = None,
) -> bool:
    """Return True when the active rail is NVIDIA NIM.

    Matches the built-in ``nvidia`` provider id and the canonical free-tier
    host ``integrate.api.nvidia.com`` (plus the NVCF alias). Custom base_url
    overrides that still point at NIM are covered by the host markers.
    """
    if (provider or "").strip().lower() == "nvidia":
        return True
    base = (base_url or "").lower()
    return any(marker in base for marker in _NVIDIA_NIM_HOST_MARKERS)


def should_defer_eager_fallback_for_nvidia_nim(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    is_rate_limited: bool = False,
    retry_count: int = 0,
    max_retries: int | None = None,
) -> bool:
    """Return True when a free-NIM 429 must retry before paid fallback.

    Eager fallback on the first 429 is correct for most providers (quota
    walls rarely clear inside the retry window). Free NVIDIA NIM is the
    exception: intermittent 429s recover within seconds, and falling through
    to a paid rail burns budget. Defer eager fallback until the retry budget
    is exhausted; after that the normal max-retries fallback path still runs.

    ``retry_count`` is the post-increment value used by the conversation loop
    (1 on the first failure). ``model`` is accepted for log/call-site parity
    and is not part of the decision.
    """
    del model  # reserved for call-site logging; detection is provider/host based
    if not is_rate_limited:
        return False
    if not is_nvidia_nim_endpoint(provider=provider, base_url=base_url):
        return False
    ceiling = max_retries if max_retries is not None else _NVIDIA_NIM_DEFAULT_RETRY_ATTEMPTS
    # Defer while retries remain (retry_count < max_retries). When
    # retry_count >= max_retries the caller falls through to the exhausted
    # path, which may still activate fallback after the budget is spent.
    return retry_count < ceiling


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    base_url: str | None,
    model: str | None,
    error: Any,
    default_wait: float,
    short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
    provider: str | None = None,
) -> tuple[float, str | None]:
    """Provider-aware rate-limit backoff.

    For most providers this returns ``default_wait`` unchanged. For Z.AI
    Coding Plan GLM-5.2 overloads, keep the first ``short_attempts`` retries on
    the normal short exponential schedule, then switch to progressively longer
    waits (30s → 60s → 90s → 120s, capped) plus light jitter.

    For free NVIDIA NIM 429s, label the wait as ``nvidia_nim_free_tier`` so
    logs carry the model/attempt context HEL-6108 requires (the delay itself
    stays on the shared exponential schedule unless Retry-After is present).

    ``attempt`` is 1-based, matching the retry loop's logged attempt number.
    Returns ``(wait_seconds, reason_label)`` where ``reason_label`` is suitable
    for status/log decoration when a provider-specific policy fired.
    """
    if is_zai_coding_overload_error(base_url=base_url, model=model, error=error):
        if attempt <= short_attempts:
            return default_wait, "zai_coding_overload_short"

        idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
        base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
        # A smaller jitter ratio keeps long waits readable while still avoiding
        # synchronized retry storms across concurrent Hermes sessions.
        return (
            jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2),
            "zai_coding_overload_long",
        )

    status = getattr(error, "status_code", None)
    if status == 429 and is_nvidia_nim_endpoint(provider=provider, base_url=base_url):
        # Prefer a slightly shorter base than the generic 2s rate-limit path
        # so free-tier hiccups clear without multi-minute hangs; still
        # exponential with jitter. Callers that already resolved Retry-After
        # pass that value as default_wait and we leave it alone.
        if default_wait and default_wait > 0:
            # Caller supplied Retry-After or a precomputed wait — keep it,
            # only stamp the policy label for observability.
            return default_wait, "nvidia_nim_free_tier"
        wait = jittered_backoff(attempt, base_delay=1.0, max_delay=30.0)
        return wait, "nvidia_nim_free_tier"

    return default_wait, None


def zai_coding_overload_retry_ceiling(short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS) -> int:
    """Retry-loop ceiling needed for the full Z.AI overload backoff schedule.

    The adaptive policy runs ``short_attempts`` short retries, then walks the
    long-backoff table one entry per subsequent attempt. The retry loop gives
    up as soon as ``retry_count >= ceiling`` — and that check runs *before* the
    attempt's backoff is computed — so the ceiling must sit one past the final
    long-backoff entry for every long tier to actually execute.

    With the default ``api_max_retries`` (3) equal to ``short_attempts`` (3),
    the loop always gave up before reaching the long tier, leaving the whole
    long-backoff schedule as dead code. Callers extend the ceiling to this
    value for Z.AI Coding overload 429s so the 30/60/90/120s waits run.
    """
    return short_attempts + len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) + 1
