"""Tests for the NBU REST API client."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import requests_mock

from nbu_exporter.client import NBUClient, mask_api_key
from nbu_exporter.config import NBUConfig


def _cfg(pagination_style: str = "auto") -> NBUConfig:
    return NBUConfig(
        host="nbu.example",
        port=1556,
        basePath="/netbackup",
        apiKey="A1Yepqrst5678c7k",
        apiVersion="3.0",
        requestTimeoutSeconds=5,
        maxRetries=0,
        retryBackoffSeconds=0,
        paginationStyle=pagination_style,
    )


def test_mask_api_key_short() -> None:
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "***"
    assert mask_api_key("A1Yepqrst5678c7k") == "A1Ye...8c7k"


def test_describe_redacts_key() -> None:
    client = NBUClient(_cfg())
    desc = client.describe()
    assert "A1Yepqrst5678c7k" not in desc
    assert "A1Ye...8c7k" in desc


def test_get_all_paginates_to_short_page() -> None:
    cfg = _cfg()
    client = NBUClient(cfg)
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            [
                {"json": {"data": [{"id": "1"}, {"id": "2"}]}},
                {"json": {"data": [{"id": "3"}]}},
            ],
        )
        out = client.get_all("/x", page_size=2, max_pages=10)
    assert [r["id"] for r in out] == ["1", "2", "3"]


def test_get_all_respects_max_pages(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _cfg()
    client = NBUClient(cfg)
    caplog.set_level(logging.WARNING, logger="nbu_exporter.client")
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            json={"data": [{"id": "1"}, {"id": "2"}]},
        )
        out = client.get_all("/x", page_size=2, max_pages=2)
    assert len(out) == 4
    assert any("max_pages" in rec.message for rec in caplog.records)


def test_get_all_passes_pagination_params() -> None:
    cfg = _cfg(pagination_style="jsonapi")
    client = NBUClient(cfg)
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            json={"data": []},
        )
        client.get_all("/x", params={"sort": "-startTime"}, page_size=50, max_pages=5)
        req = m.last_request
        assert req is not None
        assert req.qs["page[limit]"] == ["50"]
        assert req.qs["page[offset]"] == ["0"]
        assert req.qs["sort"] == ["-starttime"]


def test_pagination_style_jsonapi() -> None:
    """jsonapi mode sends page[limit] / page[offset]."""
    client = NBUClient(_cfg(pagination_style="jsonapi"))
    with requests_mock.Mocker() as m:
        m.get("https://nbu.example:1556/netbackup/x", json={"data": [{"id": "1"}]})
        client.get_all("/x", page_size=10, max_pages=1)
        req = m.last_request
        assert req is not None
        assert req.qs.get("page[limit]") == ["10"]
        assert req.qs.get("page[offset]") == ["0"]
        assert "limit" not in req.qs
        assert "offset" not in req.qs


def test_pagination_style_legacy() -> None:
    """legacy mode sends limit / offset (NBU 8.x, 9.x, 10.0)."""
    client = NBUClient(_cfg(pagination_style="legacy"))
    with requests_mock.Mocker() as m:
        m.get("https://nbu.example:1556/netbackup/x", json={"data": [{"id": "1"}]})
        client.get_all("/x", page_size=10, max_pages=1)
        req = m.last_request
        assert req is not None
        assert req.qs.get("limit") == ["10"]
        assert req.qs.get("offset") == ["0"]
        assert "page[limit]" not in req.qs
        assert "page[offset]" not in req.qs


def test_pagination_auto_falls_back_to_legacy_on_400(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """auto mode probes jsonapi first; on HTTP 400 it retries with legacy."""
    client = NBUClient(_cfg(pagination_style="auto"))
    caplog.set_level(logging.INFO, logger="nbu_exporter.client")
    with requests_mock.Mocker() as m:
        url = "https://nbu.example:1556/netbackup/x"

        def handler(request: Any, context: Any) -> dict[str, Any]:
            qs = request.qs
            if "page[limit]" in qs:
                context.status_code = 400
                return {"errorMessage": "unsupported pagination"}
            assert "limit" in qs
            context.status_code = 200
            return {"data": [{"id": "1"}]}

        m.get(url, json=handler)
        out = client.get_all("/x", page_size=10, max_pages=5)

    assert [r["id"] for r in out] == ["1"]
    assert any(
        "falling back to legacy" in rec.message for rec in caplog.records
    ), "expected an info log line announcing the fallback"
    # The fallback decision must be cached so a second call skips the probe.
    with requests_mock.Mocker() as m:
        m.get("https://nbu.example:1556/netbackup/x", json={"data": []})
        client.get_all("/x", page_size=10, max_pages=1)
        req = m.last_request
        assert req is not None
        assert "limit" in req.qs
        assert "page[limit]" not in req.qs


def test_pagination_auto_jsonapi_success_no_retry() -> None:
    """auto mode that succeeds with jsonapi must not also issue a legacy probe."""
    client = NBUClient(_cfg(pagination_style="auto"))
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            json={"data": [{"id": "1"}]},
        )
        client.get_all("/x", page_size=10, max_pages=1)
        # Exactly one HTTP call: the jsonapi-style first page.
        assert len(m.request_history) == 1
        assert m.request_history[0].qs.get("page[limit]") == ["10"]


def test_invalid_pagination_style_rejected_by_validate() -> None:
    """Validation rejects unknown values."""
    cfg = NBUConfig(
        host="h", apiKey="k", apiVersion="3.0", paginationStyle="weird"
    )
    from nbu_exporter.config import Config

    full = Config()
    full.nbu = cfg
    with pytest.raises(ValueError, match="paginationStyle"):
        full.validate()


def test_api_key_not_in_describe_url() -> None:
    cfg = _cfg()
    client = NBUClient(cfg)
    assert cfg.apiKey not in client.describe()
    assert cfg.apiKey not in client.base_url
