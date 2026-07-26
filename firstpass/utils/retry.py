"""
retry.py — LLM retry decorator with exponential backoff + jitter.

Handles transient failures from Anthropic and OpenAI APIs:
  - RateLimitError     → back off, retry
  - APIConnectionError → retry up to max_retries
  - APITimeoutError    → retry with longer delay

Source lineage: utils/retry.py (ceo-bot)
"""
from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar, Any

log = logging.getLogger("firstpass.utils.retry")


def _get_retryable_exceptions():
    """Lazily collect retryable exception classes from installed LLM SDKs."""
    excs = []
    try:
        import anthropic
        excs += [anthropic.RateLimitError, anthropic.APIConnectionError,
                 anthropic.APITimeoutError]
    except (ImportError, AttributeError):
        pass
    try:
        import openai
        excs += [openai.RateLimitError, openai.APIConnectionError,
                 openai.APITimeoutError]
    except (ImportError, AttributeError):
        pass
    return tuple(excs) if excs else (Exception,)


def llm_retry(max_retries: int = 3, base_delay: float = 2.0, jitter: float = 0.5):
    """
    Decorator: retry an LLM call on transient errors with exponential backoff.

    :param max_retries: Maximum number of retry attempts (default 3)
    :param base_delay:  Base sleep time in seconds (doubles each retry)
    :param jitter:      ±jitter added to delay to avoid thundering-herd

    Usage::

        @llm_retry(max_retries=3, base_delay=2.0)
        def call_claude(prompt: str) -> str:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            retryable = _get_retryable_exceptions()
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = base_delay * (2 ** attempt) + random.uniform(-jitter, jitter)
                    delay = max(0.1, delay)
                    log.warning(
                        f"[llm_retry] {fn.__name__} attempt {attempt+1}/{max_retries} failed: "
                        f"{type(exc).__name__}: {exc}. Retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


def http_retry(max_retries: int = 3, base_delay: float = 1.0, jitter: float = 0.3,
               status_codes: tuple = (429, 500, 502, 503, 504)):
    """
    Decorator: retry an HTTP call on transient errors / rate-limit responses.

    Checks the ``status_code`` attribute of any raised exception's response.

    :param status_codes: HTTP status codes that trigger a retry.

    Usage::

        @http_retry(max_retries=3)
        def fetch_orders() -> list:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            import requests
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except requests.RequestException as exc:
                    last_exc = exc
                    resp = getattr(exc, "response", None)
                    should_retry = resp is None or resp.status_code in status_codes
                    if not should_retry or attempt == max_retries:
                        break
                    retry_after = None
                    if resp is not None:
                        retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = base_delay * (2 ** attempt)
                    else:
                        delay = base_delay * (2 ** attempt) + random.uniform(-jitter, jitter)
                    delay = max(0.5, delay)
                    log.warning(
                        f"[http_retry] {fn.__name__} attempt {attempt+1}/{max_retries}: "
                        f"{exc}. Retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
