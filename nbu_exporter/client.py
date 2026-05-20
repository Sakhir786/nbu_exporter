"""NetBackup REST API client.

Provides a single :class:`NBUClient` that wraps :mod:`requests` with retry
logic, pagination and API-key masking for safe logging.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nbu_exporter.config import NBUConfig

LOGGER = logging.getLogger(__name__)


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

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a single GET request and return the parsed JSON body."""
        url = f"{self._base_url}{path}"
        LOGGER.debug("GET %s params=%s", url, params)
        resp = self._session.get(
            url,
            params=params,
            timeout=self._cfg.requestTimeoutSeconds,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object from {path}, got {type(data).__name__}")
        return data

    def get_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        page_size: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Walk JSON:API-style pagination and return every ``data`` record."""
        params = dict(params or {})
        offset = 0
        pages = 0
        out: list[dict[str, Any]] = []
        while True:
            page_params = dict(params)
            page_params["page[limit]"] = page_size
            page_params["page[offset]"] = offset
            payload = self.get(path, params=page_params)
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
        LOGGER.debug("get_all %s fetched %d records over %d pages", path, len(out), pages)
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
