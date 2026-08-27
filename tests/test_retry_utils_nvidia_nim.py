"""HEL-6108: free NVIDIA NIM 429 retry-before-paid-fallback helpers."""

from types import SimpleNamespace

from agent.retry_utils import (
    adaptive_rate_limit_backoff,
    is_nvidia_nim_endpoint,
    should_defer_eager_fallback_for_nvidia_nim,
)


NIM_BASE = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


def _nim_429():
    return SimpleNamespace(
        status_code=429,
        body={"error": {"message": "Too Many Requests"}},
        response=SimpleNamespace(headers={}),
    )


def test_is_nvidia_nim_endpoint_by_provider():
    assert is_nvidia_nim_endpoint(provider="nvidia", base_url=None) is True
    assert is_nvidia_nim_endpoint(provider="NVIDIA", base_url="") is True


def test_is_nvidia_nim_endpoint_by_host():
    assert is_nvidia_nim_endpoint(provider="custom", base_url=NIM_BASE) is True
    assert is_nvidia_nim_endpoint(
        provider=None, base_url="https://api.nvcf.nvidia.com/v2"
    ) is True
    assert is_nvidia_nim_endpoint(
        provider="openrouter", base_url="https://openrouter.ai/api/v1"
    ) is False


def test_defer_eager_fallback_while_retries_remain():
    assert (
        should_defer_eager_fallback_for_nvidia_nim(
            provider="nvidia",
            base_url=NIM_BASE,
            model=NIM_MODEL,
            is_rate_limited=True,
            retry_count=1,
            max_retries=3,
        )
        is True
    )
    assert (
        should_defer_eager_fallback_for_nvidia_nim(
            provider="nvidia",
            base_url=NIM_BASE,
            model=NIM_MODEL,
            is_rate_limited=True,
            retry_count=2,
            max_retries=3,
        )
        is True
    )


def test_do_not_defer_when_retry_budget_exhausted():
    """After max_retries, fall through so max-retries fallback can engage."""
    assert (
        should_defer_eager_fallback_for_nvidia_nim(
            provider="nvidia",
            base_url=NIM_BASE,
            model=NIM_MODEL,
            is_rate_limited=True,
            retry_count=3,
            max_retries=3,
        )
        is False
    )


def test_do_not_defer_for_non_nim_providers():
    assert (
        should_defer_eager_fallback_for_nvidia_nim(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="x-ai/grok-4.5",
            is_rate_limited=True,
            retry_count=1,
            max_retries=3,
        )
        is False
    )


def test_do_not_defer_when_not_rate_limited():
    assert (
        should_defer_eager_fallback_for_nvidia_nim(
            provider="nvidia",
            base_url=NIM_BASE,
            model=NIM_MODEL,
            is_rate_limited=False,
            retry_count=1,
            max_retries=3,
        )
        is False
    )


def test_adaptive_backoff_labels_nvidia_nim_free_tier():
    wait, policy = adaptive_rate_limit_backoff(
        1,
        base_url=NIM_BASE,
        model=NIM_MODEL,
        error=_nim_429(),
        default_wait=2.5,
        provider="nvidia",
    )
    assert policy == "nvidia_nim_free_tier"
    assert wait == 2.5


def test_adaptive_backoff_does_not_label_other_providers():
    wait, policy = adaptive_rate_limit_backoff(
        1,
        base_url="https://openrouter.ai/api/v1",
        model="x-ai/grok-4.5",
        error=_nim_429(),
        default_wait=2.5,
        provider="openrouter",
    )
    assert policy is None
    assert wait == 2.5
