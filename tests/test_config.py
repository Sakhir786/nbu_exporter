"""Tests for the YAML config loader and validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from nbu_exporter.config import Config, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


VALID = """
nbu:
  host: nbu.example.com
  apiKey: my-real-key
  apiVersion: "3.0"
"""


def test_load_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.nbu.host == "nbu.example.com"
    assert cfg.nbu.apiKey == "my-real-key"
    assert cfg.server.listenAddress == "0.0.0.0:2112"
    assert cfg.collectors.jobs.enabled is True


def test_missing_host_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="host"):
        load_config(_write(tmp_path, 'nbu:\n  apiKey: x\n  apiVersion: "3.0"\n'))


def test_missing_api_key_fails(tmp_path: Path) -> None:
    body = 'nbu:\n  host: h\n  apiVersion: "3.0"\n'
    with pytest.raises(ValueError, match="apiKey"):
        load_config(_write(tmp_path, body))


def test_placeholder_api_key_rejected(tmp_path: Path) -> None:
    body = 'nbu:\n  host: h\n  apiKey: REPLACE_WITH_NBU_API_KEY\n  apiVersion: "3.0"\n'
    with pytest.raises(ValueError, match="apiKey"):
        load_config(_write(tmp_path, body))


def test_all_collectors_disabled_fails(tmp_path: Path) -> None:
    body = (
        VALID
        + """
collectors:
  jobs: {enabled: false}
  jobStates: {enabled: false}
  clients: {enabled: false}
  policies: {enabled: false}
  storageUnits: {enabled: false}
  diskPools: {enabled: false}
  storageServers: {enabled: false}
  msdp: {enabled: false}
  catalog: {enabled: false}
"""
    )
    with pytest.raises(ValueError, match="collector"):
        load_config(_write(tmp_path, body))


def test_defaults_applied_for_missing_collector_block(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.collectors.diskPools.upStateValues == [2, 1]
    assert cfg.collectors.catalog.policyTypeValues == ["NBU-Catalog"]
    assert cfg.collectors.jobs.lookbackHours == 24


def test_non_positive_ttl_rejected(tmp_path: Path) -> None:
    body = VALID + "collectors:\n  jobs:\n    cacheTTLSeconds: 0\n"
    with pytest.raises(ValueError, match="cacheTTL"):
        load_config(_write(tmp_path, body))


def test_default_config_invalid() -> None:
    """A bare-defaults Config has no host/apiKey and must fail validation."""
    cfg = Config()
    with pytest.raises(ValueError):
        cfg.validate()
