"""NetBackup REST API client.

Provides a single :class:`NBUClient` that wraps :mod:`requests` with retry
logic, pagination and API-key masking for safe logging.

Pagination
----------
NetBackup changed its pagination query parameters between releases:

* "jsonapi" — ``page[limit]`` and ``page[offset]`` (NBU 10.1 and newer)
* "legacy"  — ``limit`` and ``offset`` (NBU 8.x, 9.x, 10.0)
* "auto"    — try jsonapi first; on HTTP 400 from the master, fall back to
              legacy. The detected style is then cached per endpoint so
              subsequent pages do not retry.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nbu_exporter.config import NBUConfig

LOGGER = logging.getLogger(__name__)

PAGINATION_JSONAPI = "jsonapi"
PAGINATION_LEGACY = "legacy"
PAGINATION_AUTO = "auto"


def mask_api_key(key: str) -> str:
    """Return a redacted view of an API key safe to include in logs."""
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


class NBUClient:
    """Thin wrapper around :class:`requests.Session` for the NBU REST API."""

    def __init__(self, cfg: NBUConfig) -> None:
        self._cfg = cfg
        self._base_url = f"{cfg.scheme}://{cfg.host}:{cfg.port}{cfg.basePath}"
        self._session = self._build_session(cfg)
        # Per-endpoint pagination-style cache, used only in auto mode so we
        # do not retry the jsonapi→legacy probe on every page.
        self._pagination_cache: dict[str, str] = {}
        self._pagination_lock = threading.Lock()

    @staticmethod
    def _build_session(cfg: NBUConfig) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": cfg.apiKey,
                "Content-Type": cfg.contentType,
                "Accept": cfg.contentType,
            }
        )
        if cfg.insecureSkipVerify:
            session.verify = False
        elif cfg.caCertFile:
            session.verify = cfg.caCertFile
        else:
            session.verify = True

        retry = Retry(
            total=cfg.maxRetries,
            backoff_factor=cfg.retryBackoffSeconds,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    @property
    def base_url(self) -> str:
        """Return the constructed base URL (without trailing slash)."""
        return self._base_url

    def masked_api_key(self) -> str:
        """Return the configured API key with the middle redacted."""
        return mask_api_key(self._cfg.apiKey)

    def _raw_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> requests.Response:
        """Perform a GET without raising on 4xx; caller inspects status."""
        url = f"{self._base_url}{path}"
        LOGGER.debug("GET %s params=%s", url, params)
        return self._session.get(
            url,
            params=params,
            timeout=self._cfg.requestTimeoutSeconds,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a single GET request and return the parsed JSON body."""
        resp = self._raw_get(path, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object from {path}, got {type(data).__name__}")
        return data

    @staticmethod
    def _page_params(style: str, page_size: int, offset: int) -> dict[str, Any]:
        """Return the per-page query params for a given pagination style."""
        if style == PAGINATION_LEGACY:
            return {"limit": page_size, "offset": offset}
        return {"page[limit]": page_size, "page[offset]": offset}

    def _initial_style(self, path: str) -> str:
        """Return the style to try first for ``path`` (honours auto-cache)."""
        configured = self._cfg.paginationStyle
        if configured != PAGINATION_AUTO:
            return configured
        with self._pagination_lock:
            return self._pagination_cache.get(path, PAGINATION_JSONAPI)

    def _remember_style(self, path: str, style: str) -> None:
        """Cache the working pagination style for ``path`` (auto mode only)."""
        if self._cfg.paginationStyle != PAGINATION_AUTO:
            return
        with self._pagination_lock:
            self._pagination_cache[path] = style

    def _fetch_page(
        self,
        path: str,
        base_params: dict[str, Any],
        style: str,
        page_size: int,
        offset: int,
    ) -> tuple[dict[str, Any], str]:
        """Fetch one page; in auto mode, retry once with legacy on HTTP 400.

        Returns the parsed JSON body and the style that actually worked.
        """
        page_params = dict(base_params)
        page_params.update(self._page_params(style, page_size, offset))
        resp = self._raw_get(path, params=page_params)
        if (
            resp.status_code == 400
            and self._cfg.paginationStyle == PAGINATION_AUTO
            and style == PAGINATION_JSONAPI
        ):
            LOGGER.info(
                "auto pagination: %s rejected jsonapi params (400), falling back to legacy",
                path,
            )
            style = PAGINATION_LEGACY
            page_params = dict(base_params)
            page_params.update(self._page_params(style, page_size, offset))
            resp = self._raw_get(path, params=page_params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object from {path}, got {type(data).__name__}")
        return data, style

    def get_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        page_size: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Walk paginated results and return every ``data`` record.

        Pagination query parameters are chosen by ``nbu.paginationStyle``.
        In auto mode the style is probed on the first page and the result
        is cached so subsequent pages do not retry.
        """
        params = dict(params or {})
        style = self._initial_style(path)
        offset = 0
        pages = 0
        out: list[dict[str, Any]] = []
        while True:
            payload, style = self._fetch_page(path, params, style, page_size, offset)
            self._remember_style(path, style)
            page_data = payload.get("data")
            if not isinstance(page_data, list):
                LOGGER.warning("response from %s has no list `data` field", path)
                break
            out.extend(page_data)
            pages += 1
            if len(page_data) < page_size:
                break
            if pages >= max_pages:
                LOGGER.warning(
                    "get_all(%s) hit max_pages=%d; results may be truncated",
                    path,
                    max_pages,
                )
                break
            offset += page_size
        LOGGER.debug(
            "get_all %s fetched %d records over %d pages (style=%s)",
            path,
            len(out),
            pages,
            style,
        )
        return out

    def health(self) -> bool:
        """Lightweight reachability probe — returns False on any error."""
        url = f"{self._base_url}/ping"
        try:
            resp = self._session.get(url, timeout=self._cfg.requestTimeoutSeconds)
            return resp.status_code < 500
        except requests.RequestException as exc:
            LOGGER.debug("health probe failed: %s", exc)
            return False

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def describe(self) -> str:
        """Return a redacted summary suitable for logging at startup."""
        params = urlencode({"apiVersion": self._cfg.apiVersion})
        return f"{self._base_url}?{params} key={self.masked_api_key()}"
