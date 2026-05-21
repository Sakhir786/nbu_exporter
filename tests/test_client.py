"""Tests for the NBU REST API client."""

from __future__ import annotations

import logging

import pytest
import requests_mock

from nbu_exporter.client import NBUClient, mask_api_key
from nbu_exporter.config import NBUConfig


def _cfg() -> NBUConfig:
    return NBUConfig(
        host="nbu.example",
        port=1556,
        basePath="/netbackup",
        apiKey="A1Yepqrst5678c7k",
        apiVersion="3.0",
        requestTimeoutSeconds=5,
        maxRetries=0,
        retryBackoffSeconds=0,
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
    client = NBUClient(_cfg())
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            [
                {"json": {"data": [{"id": "1"}, {"id": "2"}]}},
                {"json": {"data": [{"id": "3"}]}},
            ],
        )
        out = client.get_all("/x", page_size=2, max_pages=10, style="jsonapi")
    assert [r["id"] for r in out] == ["1", "2", "3"]


def test_get_all_respects_max_pages(caplog: pytest.LogCaptureFixture) -> None:
    client = NBUClient(_cfg())
    caplog.set_level(logging.WARNING, logger="nbu_exporter.client")
    with requests_mock.Mocker() as m:
        m.get(
            "https://nbu.example:1556/netbackup/x",
            json={"data": [{"id": "1"}, {"id": "2"}]},
        )
        out = client.get_all("/x", page_size=2, max_pages=2, style="jsonapi")
    assert len(out) == 4
    assert any("max_pages" in rec.message for rec in caplog.records)


def test_pagination_style_jsonapi() -> None:
    """jsonapi style sends page[limit] / page[offset]."""
    client = NBUClient(_cfg())
    with requests_mock.Mocker() as m:
        m.get("https://nbu.example:1556/netbackup/x", json={"data": [{"id": "1"}]})
        client.get_all(
            "/x",
            params={"sort": "-startTime"},
            page_size=10,
            max_pages=1,
            style="jsonapi",
        )
        req = m.last_request
        assert req is not None
        assert req.qs.get("page[limit]") == ["10"]
        assert req.qs.get("page[offset]") == ["0"]
        assert req.qs.get("sort") == ["-starttime"]
        assert "limit" not in req.qs
        assert "offset" not in req.qs


def test_pagination_style_legacy() -> None:
    """legacy style sends limit / offset (NBU 8.x, 9.x)."""
    client = NBUClient(_cfg())
    with requests_mock.Mocker() as m:
        m.get("https://nbu.example:1556/netbackup/x", json={"data": [{"id": "1"}]})
        client.get_all(
            "/x",
            params={"sort": "-startTime"},
            page_size=10,
            max_pages=1,
            style="legacy",
        )
        req = m.last_request
        assert req is not None
        assert req.qs.get("limit") == ["10"]
        assert req.qs.get("offset") == ["0"]
        assert req.qs.get("sort") == ["-starttime"]
        assert "page[limit]" not in req.qs
        assert "page[offset]" not in req.qs


def test_get_all_rejects_unknown_style() -> None:
    client = NBUClient(_cfg())
    with pytest.raises(ValueError, match="pagination style"):
        client.get_all("/x", page_size=10, max_pages=1, style="weird")


def test_legacy_pagination_walks_multiple_pages() -> None:
    """Pagination walk must work identically for legacy style."""
    client = NBUClient(_cfg())
    with requests_mock.Mocker() as m:
        url = "https://nbu.example:1556/netbackup/x"
        m.get(
            url,
            [
                {"json": {"data": [{"id": "1"}, {"id": "2"}]}},
                {"json": {"data": [{"id": "3"}]}},
            ],
        )
        out = client.get_all("/x", page_size=2, max_pages=10, style="legacy")
        assert [r["id"] for r in out] == ["1", "2", "3"]
        first, second = m.request_history
        assert first.qs.get("offset") == ["0"]
        assert second.qs.get("offset") == ["2"]


def test_api_key_not_in_describe_url() -> None:
    cfg = _cfg()
    client = NBUClient(cfg)
    assert cfg.apiKey not in client.describe()
    assert cfg.apiKey not in client.base_url
