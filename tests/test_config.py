"""Tests for the YAML config loader and validator."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from nbu_exporter.config import Config, load_config, load_config_with_notes

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config.yaml.example"


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


def test_default_pagination_block(tmp_path: Path) -> None:
    """nbu.pagination block has sensible NBU-10.0-safe defaults."""
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.nbu.pagination.pageSize == 100
    assert cfg.nbu.pagination.maxPages == 200
    assert cfg.nbu.pagination.style == "jsonapi"


def test_pagination_block_invalid_style_rejected(tmp_path: Path) -> None:
    body = 'nbu:\n  host: h\n  apiKey: k\n  apiVersion: "3.0"\n  pagination:\n    style: weird\n'
    with pytest.raises(ValueError, match="pagination.style"):
        load_config(_write(tmp_path, body))


def test_pagination_block_non_positive_rejected(tmp_path: Path) -> None:
    body = 'nbu:\n  host: h\n  apiKey: k\n  apiVersion: "3.0"\n  pagination:\n    pageSize: 0\n'
    with pytest.raises(ValueError, match="pagination.pageSize"):
        load_config(_write(tmp_path, body))


def test_collector_overrides_pagination(tmp_path: Path) -> None:
    """Per-collector pageSize/maxPages override nbu.pagination."""
    body = """
nbu:
  host: nbu.example.com
  apiKey: my-real-key
  apiVersion: "3.0"
  pagination:
    pageSize: 100
    maxPages: 200
    style: jsonapi
collectors:
  jobs:
    pageSize: 500
    maxPages: 50
"""
    cfg = load_config(_write(tmp_path, body))
    ps, mp, style = cfg.resolve_pagination(
        cfg.collectors.jobs.pageSize, cfg.collectors.jobs.maxPages
    )
    assert (ps, mp, style) == (500, 50, "jsonapi")
    # A collector that didn't override inherits nbu.pagination defaults.
    ps2, mp2, style2 = cfg.resolve_pagination(
        cfg.collectors.clients.pageSize, cfg.collectors.clients.maxPages
    )
    assert (ps2, mp2, style2) == (100, 200, "jsonapi")


def test_clients_block_validation(tmp_path: Path) -> None:
    """Empty client-collector knobs are rejected by validate()."""
    for key in (
        "hostsEndpoint",
        "hostsResponseKey",
        "hostTypeField",
    ):
        body = VALID + f"collectors:\n  clients:\n    {key}: ''\n"
        with pytest.raises(ValueError, match=f"clients.{key}"):
            load_config(_write(tmp_path, body))

    body = VALID + "collectors:\n  clients:\n    clientHostTypeValues: []\n"
    with pytest.raises(ValueError, match="clients.clientHostTypeValues"):
        load_config(_write(tmp_path, body))


def test_clients_defaults_for_nbu_10_0(tmp_path: Path) -> None:
    """Out-of-the-box defaults match NBU 10.0's /config/hosts shape."""
    cfg = load_config(_write(tmp_path, VALID))
    c = cfg.collectors.clients
    assert c.hostsEndpoint == "/config/hosts"
    assert c.hostsResponseKey == "hosts"
    assert c.hostTypeField == "nbuHostType"
    assert c.clientHostTypeValues == ["CLIENT"]


def test_load_config_with_notes_reports_missing_pagination_block(tmp_path: Path) -> None:
    """nbu.pagination missing from YAML triggers an operator-visible note."""
    cfg, notes = load_config_with_notes(_write(tmp_path, VALID))
    assert any("nbu.pagination" in n for n in notes), notes
    note = next(n for n in notes if "nbu.pagination" in n)
    assert "pageSize=100" in note
    assert "style=jsonapi" in note


def test_load_config_with_notes_silent_when_pagination_present(tmp_path: Path) -> None:
    """When nbu.pagination is explicit, no fallback note is emitted."""
    body = (
        'nbu:\n  host: h\n  apiKey: k\n  apiVersion: "3.0"\n'
        "  pagination:\n    pageSize: 500\n    maxPages: 200\n    style: jsonapi\n"
    )
    _, notes = load_config_with_notes(_write(tmp_path, body))
    assert not any("nbu.pagination" in n for n in notes), notes


def test_resolve_pagination_uses_legacy_for_nbu_8_or_9(tmp_path: Path) -> None:
    body = (
        'nbu:\n  host: h\n  apiKey: k\n  apiVersion: "3.0"\n'
        "  pagination:\n    style: legacy\n    pageSize: 100\n"
    )
    cfg = load_config(_write(tmp_path, body))
    _, _, style = cfg.resolve_pagination(None, None)
    assert style == "legacy"


# --------------------------------------------------------------------------- #
# config.yaml.example completeness — locks the shipped example to the schema
# so a new dataclass field cannot land without being documented.
# --------------------------------------------------------------------------- #


def _walk_dataclass_paths(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    for fld in dataclasses.fields(obj):
        path = f"{prefix}.{fld.name}" if prefix else fld.name
        val = getattr(obj, fld.name)
        if dataclasses.is_dataclass(val):
            out.extend(_walk_dataclass_paths(val, path))
        else:
            out.append(path)
    return out


def _dotted_present(d: Any, dotted: str) -> bool:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_example_config_covers_every_dataclass_field() -> None:
    """Every Config dataclass field must appear in config.yaml.example."""
    with EXAMPLE_CONFIG.open() as fh:
        example = yaml.safe_load(fh)
    missing = [
        p for p in _walk_dataclass_paths(Config()) if not _dotted_present(example, p)
    ]
    assert not missing, (
        "config.yaml.example is missing these keys (add them with explanatory "
        f"comments so operators see every available knob): {missing}"
    )


def test_example_config_parses_and_validates(tmp_path: Path) -> None:
    """The shipped example, with placeholders replaced, must load cleanly."""
    raw = EXAMPLE_CONFIG.read_text()
    patched = raw.replace("REPLACE_WITH_NBU_API_KEY", "real-api-key").replace(
        "nbu-master.example.com", "nbu.real.example.com"
    )
    path = tmp_path / "config.yaml"
    path.write_text(patched)
    cfg = load_config(path)
    assert cfg.nbu.host == "nbu.real.example.com"
    assert cfg.nbu.apiKey == "real-api-key"
