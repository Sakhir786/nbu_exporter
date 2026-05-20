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
    cfg = _cfg()
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


def test_api_key_not_in_describe_url() -> None:
    cfg = _cfg()
    client = NBUClient(cfg)
    assert cfg.apiKey not in client.describe()
    assert cfg.apiKey not in client.base_url
