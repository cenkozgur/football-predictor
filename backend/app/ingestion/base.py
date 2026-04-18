"""Shared HTTP helper for DIY scrapers — polite defaults, retries, and rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


_JINA_MARKER = "Markdown Content:"


def proxied(url: str, proxy_prefix: str) -> str:
    """Prefix `url` with `proxy_prefix` if a proxy is configured.

    The Jina Reader format is literally `https://r.jina.ai/<full-url>`.
    """
    if not proxy_prefix:
        return url
    return proxy_prefix.rstrip("/") + "/" + url


def strip_jina_header(text: str) -> str:
    """Remove the Jina Reader markdown wrapper from a text response.

    Jina prefixes responses with a small block like:

        Title:

        URL Source: ...

        Published Time: ...

        Markdown Content:
        <actual body>

    For CSV bodies we need only the part after "Markdown Content:". When the
    text wasn't fetched via Jina (no marker), it is returned unchanged.
    """
    idx = text.find(_JINA_MARKER)
    if idx == -1:
        return text
    return text[idx + len(_JINA_MARKER):].lstrip("\n").lstrip()

DEFAULT_UA = (
    "football-predictor/0.1 (educational, contact: local)"
)


@dataclass
class HttpFetcher:
    user_agent: str = DEFAULT_UA
    timeout: float = 90.0  # Jina Reader proxy can be slow on first hit
    retries: int = 3
    backoff_seconds: float = 2.0
    rate_limit_seconds: float = 1.0

    def __post_init__(self) -> None:
        self._last_fetch: float = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
            follow_redirects=True,
        )

    def get(self, url: str) -> httpx.Response:
        # Respect rate limit
        elapsed = time.monotonic() - self._last_fetch
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                self._last_fetch = time.monotonic()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * attempt)
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()
