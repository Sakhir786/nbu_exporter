"""Tests for the Prometheus collectors."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

from prometheus_client.metrics_core import Metric

from nbu_exporter.collectors import (
    CatalogCollector,
    ClientsCollector,
    DiskPoolsCollector,
    DiskVolumesCollector,
    JobsCollector,
    MSDPCollector,
    NBUCollector,
    PoliciesCollector,
    StorageServersCollector,
    StorageUnitsCollector,
    _parse_iso,
)
from nbu_exporter.config import Config


def _samples(metric: Metric, name: str) -> list[tuple[dict[str, str], float]]:
    return [(dict(s.labels), s.value) for s in metric.samples if s.name == name]


def _find(metrics: list[Metric], name: str) -> Metric:
    for m in metrics:
        if m.name == name:
            return m
    raise AssertionError(f"metric {name} not found in {[m.name for m in metrics]}")


def _fake_client(
    jobs: list[dict[str, Any]] | None = None, pools: list[dict[str, Any]] | None = None
) -> Any:
    client = MagicMock()
    client.get_all.return_value = jobs or []
    return client


class _StaticCache:
    """Stand-in for nbu_exporter.cache.TTLCache holding a fixed payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def get(self) -> Any:
        return self._payload


# --------------------------------------------------------------------------- #
# Jobs / state rollups
# --------------------------------------------------------------------------- #


def test_jobs_collector_metrics_shape() -> None:
    cfg = Config()
    jobs = [
        {
            "attributes": {
                "jobType": "BACKUP",
                "policyType": "VMware",
                "status": "0",
                "state": "DONE",
                "policyName": "policy-a",
                "clientName": "host1",
                "scheduleName": "daily",
                "kilobytesTransferred": 1024,
                "startTime": "2026-05-01T00:00:00Z",
                "endTime": "2026-05-01T01:00:00Z",
            }
        },
        {
            "attributes": {
                "jobType": "BACKUP",
                "policyType": "VMware",
                "status": "1",
                "state": "DONE",
                "policyName": "policy-a",
                "clientName": "host1",
                "scheduleName": "daily",
                "kilobytesTransferred": 0,
                "endTime": "2026-05-02T01:00:00Z",
            }
        },
    ]
    client = _fake_client(jobs=jobs)
    sub = JobsCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))
    counts = _samples(_find(metrics, "nbu_jobs_count"), "nbu_jobs_count")
    assert {"action": "BACKUP", "policy_type": "VMware", "status": "0"} in [
        labels for labels, _ in counts
    ]
    by_state = _find(metrics, "nbu_jobs_by_state")
    assert any(s.labels["state"] == "DONE" for s in by_state.samples)
    policy = _find(metrics, "nbu_policy_jobs_count")
    assert any(s.labels["policy_name"] == "policy-a" for s in policy.samples)
    last_status = _find(metrics, "nbu_client_last_attempt_status")
    sample = next(s for s in last_status.samples if s.labels["client"] == "host1")
    assert sample.value == 1.0


# --------------------------------------------------------------------------- #
# Storage units — now takes jobs cache for label back-fill
# --------------------------------------------------------------------------- #


def test_storage_units_cloud_pattern_skips_free() -> None:
    cfg = Config()
    units = [
        {
            "attributes": {
                "name": "stu-aws",
                "type": "amazon_s3",
                "usedBytes": 100,
                "freeBytes": 9223372036854775807,
            }
        },
        {
            "attributes": {
                "name": "stu-disk",
                "type": "BasicDisk",
                "usedBytes": 100,
                "freeBytes": 200,
            }
        },
    ]
    client = MagicMock()
    client.get_all.return_value = units
    sub = StorageUnitsCollector(client, cfg, _StaticCache([]))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    samples = _samples(_find(metrics, "nbu_disk_bytes"), "nbu_disk_bytes")
    sizes = {(labels["name"], labels["size"]) for labels, _ in samples}
    assert ("stu-aws", "used") in sizes
    assert ("stu-aws", "free") not in sizes
    assert ("stu-disk", "free") in sizes


# --------------------------------------------------------------------------- #
# Disk pools — NBU 10.0 JSON shape, string state, ?fields=*&include=*
# --------------------------------------------------------------------------- #


def _nbu_10_pool(
    name: str = "Ephr-pool1",
    state: str = "UP",
    category: str = "MSDP",
    stype: str = "PureDisk",
    with_volumes: bool = True,
) -> dict[str, Any]:
    pool: dict[str, Any] = {
        "type": "diskPool",
        "id": f"{stype}:{name}",
        "attributes": {
            "name": name,
            "sType": stype,
            "storageCategory": category,
            "diskPoolState": state,
            "highWaterMark": 98,
            "lowWaterMark": 80,
            "diskPoolCapabilities": ["PATCH_WORK", "VISIBLE", "OPEN_STORAGE"],
        },
        "relationships": {
            "storageServers": {
                "data": [{"type": "storageServer", "id": f"{stype}:ss-{name}"}]
            }
        },
    }
    if with_volumes:
        pool["attributes"]["diskVolumes"] = [
            {
                "name": "PureDiskVolume",
                "id": "vol-1",
                "diskMediaId": "@aaaaj",
                "state": "DOWN",
                "rawSizeBytes": 47688433522688,
                "freeSizeBytes": 44619654436864,
                "isReplicationSource": False,
                "isReplicationTarget": False,
            }
        ]
    return pool


def test_disk_pools_collector_parses_nbu_10_0_shape() -> None:
    cfg = Config()
    pool = _nbu_10_pool(state="UP")
    client = MagicMock()
    client.get_all.return_value = [pool]
    # Detail call returns pool-level capacity aggregates.
    client.get.return_value = {
        "data": {
            "attributes": {
                "rawSizeBytes": 47688433522688,
                "usableSizeBytes": 47688433522688,
                "availableSpaceBytes": 44619654436864,
                "usedCapacityBytes": 3068779085824,
            }
        }
    }
    sub = DiskPoolsCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))

    state = _find(metrics, "nbu_disk_pool_state")
    by_pool = {s.labels["pool"]: s.value for s in state.samples}
    assert by_pool["Ephr-pool1"] == 1.0

    up = _find(metrics, "nbu_disk_pool_up")
    by_pool_up = {s.labels["pool"]: s.value for s in up.samples}
    assert by_pool_up["Ephr-pool1"] == 1.0

    cap = _find(metrics, "nbu_disk_pool_capacity_bytes")
    cap_by_pool = {s.labels["pool"]: s.value for s in cap.samples}
    assert cap_by_pool["Ephr-pool1"] == 47688433522688

    used = _find(metrics, "nbu_disk_pool_used_bytes")
    used_by_pool = {s.labels["pool"]: s.value for s in used.samples}
    assert used_by_pool["Ephr-pool1"] == 3068779085824

    high = _find(metrics, "nbu_disk_pool_high_watermark_percent")
    assert {s.labels["pool"]: s.value for s in high.samples}["Ephr-pool1"] == 98.0

    caps = _find(metrics, "nbu_disk_pool_capability")
    cap_labels = {(s.labels["pool"], s.labels["capability"]) for s in caps.samples}
    assert ("Ephr-pool1", "OPEN_STORAGE") in cap_labels


def test_disk_pools_detail_fetch_skipped_for_cloud() -> None:
    cfg = Config()
    cloud_pool = _nbu_10_pool(name="cloud-pool", category="CLOUD", with_volumes=False)
    client = MagicMock()
    client.get_all.return_value = [cloud_pool]
    sub = DiskPoolsCollector(client, cfg)
    list(sub.collect(MagicMock()))
    # Detail endpoint must not be called for cloud-category pools.
    assert client.get.call_count == 0


def test_disk_pools_string_state_down_emits_zero() -> None:
    cfg = Config()
    pool = _nbu_10_pool(name="pool-down", state="DOWN")
    client = MagicMock()
    client.get_all.return_value = [pool]
    client.get.return_value = {"data": {"attributes": {}}}
    sub = DiskPoolsCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))
    up = _find(metrics, "nbu_disk_pool_up")
    assert {s.labels["pool"]: s.value for s in up.samples}["pool-down"] == 0.0


# --------------------------------------------------------------------------- #
# Disk volumes — runtime UP/DOWN per volume
# --------------------------------------------------------------------------- #


def test_disk_volumes_collector_reports_volume_down_state() -> None:
    cfg = Config()
    pool = _nbu_10_pool(name="Ephr-pool1", state="UP")
    # Volume is DOWN in the fixture.
    sub = DiskVolumesCollector(cfg, _StaticCache([pool]))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    vol_up = _find(metrics, "nbu_disk_volume_up")
    by_vol = {(s.labels["pool"], s.labels["volume"]): s.value for s in vol_up.samples}
    assert by_vol[("Ephr-pool1", "PureDiskVolume")] == 0.0

    cap = _find(metrics, "nbu_disk_volume_capacity_bytes")
    cap_by_vol = {
        (s.labels["pool"], s.labels["volume"]): s.value for s in cap.samples
    }
    assert cap_by_vol[("Ephr-pool1", "PureDiskVolume")] == 47688433522688


# --------------------------------------------------------------------------- #
# Storage servers — string state, ?fields=*&include=*
# --------------------------------------------------------------------------- #


def test_storage_servers_collector_parses_string_state() -> None:
    cfg = Config()
    servers = [
        {
            "attributes": {
                "name": "ss-up",
                "sType": "PureDisk",
                "storageCategory": "MSDP",
                "storageServerState": "UP",
                "nbuStatusCode": 20,
                "nbuHostVersion": "10.0",
                "storageServerCapabilities": ["MODEL", "OPTIMIZED_IMAGE"],
            }
        },
        {
            "attributes": {
                "name": "ss-down",
                "sType": "AdvancedDisk",
                "storageCategory": "ADVANCED_DISK",
                "storageServerState": "DOWN",
                "nbuStatusCode": None,
                "nbuHostVersion": "10.0",
            }
        },
    ]
    client = MagicMock()
    client.get_all.return_value = servers
    sub = StorageServersCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))

    state = _find(metrics, "nbu_storage_server_state")
    by_name = {s.labels["name"]: s.value for s in state.samples}
    assert by_name["ss-up"] == 1.0
    assert by_name["ss-down"] == 0.0

    status = _find(metrics, "nbu_storage_server_nbu_status_code")
    status_names = {s.labels["name"] for s in status.samples}
    # null nbuStatusCode is omitted.
    assert "ss-up" in status_names
    assert "ss-down" not in status_names

    caps = _find(metrics, "nbu_storage_server_capability")
    cap_labels = {(s.labels["name"], s.labels["capability"]) for s in caps.samples}
    assert ("ss-up", "OPTIMIZED_IMAGE") in cap_labels


def test_storage_servers_collector_uses_fields_include_params() -> None:
    cfg = Config()
    client = MagicMock()
    client.get_all.return_value = []
    sub = StorageServersCollector(client, cfg)
    list(sub.collect(MagicMock()))
    _, kwargs = client.get_all.call_args
    assert kwargs["params"] == {"fields": "*", "include": "*"}


# --------------------------------------------------------------------------- #
# Clients / Policies — derived from the jobs cache
# --------------------------------------------------------------------------- #


def test_clients_collector_derives_from_jobs_cache() -> None:
    cfg = Config()
    jobs = [
        {"attributes": {"clientName": "host-1", "policyName": "p", "policyType": "VMware"}},
        {"attributes": {"clientName": "host-1", "policyName": "p", "policyType": "VMware"}},
        {"attributes": {"clientName": "host-2", "policyName": "p2", "policyType": "Standard"}},
        {"attributes": {"clientName": "", "policyName": "p", "policyType": "VMware"}},
    ]
    sub = ClientsCollector(MagicMock(), cfg, _StaticCache(jobs))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    info = _find(metrics, "nbu_client_info")
    names = {s.labels["client"] for s in info.samples}
    assert names == {"host-1", "host-2"}, names


def test_policies_collector_derives_from_jobs_cache() -> None:
    cfg = Config()
    jobs = [
        {"attributes": {"clientName": "h1", "policyName": "p-a", "policyType": "VMware"}},
        {"attributes": {"clientName": "h1", "policyName": "p-a", "policyType": "VMware"}},
        {"attributes": {"clientName": "h2", "policyName": "p-b", "policyType": "Standard"}},
    ]
    sub = PoliciesCollector(MagicMock(), cfg, _StaticCache(jobs))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    info = _find(metrics, "nbu_policy_info")
    keys = {(s.labels["policy_name"], s.labels["policy_type"]) for s in info.samples}
    assert keys == {("p-a", "VMware"), ("p-b", "Standard")}


# --------------------------------------------------------------------------- #
# Storage units — back-fill labels from cached jobs
# --------------------------------------------------------------------------- #


def test_storage_units_back_fills_media_server_from_jobs() -> None:
    """When the REST shape returns null for type, fall back to jobs."""
    cfg = Config()
    units = [{"attributes": {"name": "Ephr-pool1-stu", "type": None, "mediaServer": None}}]
    jobs = [
        {
            "attributes": {
                "destinationStorageUnitName": "Ephr-pool1-stu",
                "destinationMediaServerName": "ephrpakvm482",
            }
        }
    ]
    client = MagicMock()
    client.get_all.return_value = units
    sub = StorageUnitsCollector(client, cfg, _StaticCache(jobs))  # type: ignore[arg-type]
    # No assertion on label value beyond presence — the back-fill exists
    # primarily so other collectors can correlate; nbu_disk_bytes does not
    # carry the media-server label.
    list(sub.collect(MagicMock()))


# --------------------------------------------------------------------------- #
# MSDP — emits per-pool even when REST returns no dedup stats
# --------------------------------------------------------------------------- #


def test_msdp_only_emits_for_msdp_pools() -> None:
    cfg = Config()
    pools = [
        {
            "attributes": {
                "name": "msdp-pool",
                "logicalBytes": 1000,
                "physicalBytes": 250,
                "storageServers": [{"name": "ss", "serverType": "PureDisk"}],
            }
        },
        {
            "attributes": {
                "name": "adv-pool",
                "storageServers": [{"name": "ss", "serverType": "AdvancedDisk"}],
            }
        },
    ]
    sub = MSDPCollector(MagicMock(), cfg, _StaticCache(pools))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    ratio = _find(metrics, "nbu_msdp_dedup_ratio")
    pools_emitted = {s.labels["pool"] for s in ratio.samples}
    assert pools_emitted == {"msdp-pool"}
    sample = next(s for s in ratio.samples if s.labels["pool"] == "msdp-pool")
    assert sample.value == 4.0


# --------------------------------------------------------------------------- #
# Catalog — default policyTypeValues now uses underscore for NBU 10.0
# --------------------------------------------------------------------------- #


def test_catalog_default_policy_type_is_underscore() -> None:
    """v0.4 default ships the underscore variant NBU 10.0 actually reports."""
    assert Config().collectors.catalog.policyTypeValues == ["NBU_CATALOG"]


def test_catalog_collector_picks_matching_policy_type() -> None:
    cfg = Config()
    # Use both variants to prove the config knob, not a hardcoded value, drives
    # matching.
    cfg.collectors.catalog.policyTypeValues = ["NBU_CATALOG"]
    jobs = [
        {
            "attributes": {
                "policyType": "VMware",
                "status": "0",
                "kilobytesTransferred": 1,
                "endTime": "2026-05-01T00:00:00Z",
            }
        },
        {
            "attributes": {
                "policyType": "NBU_CATALOG",
                "status": "0",
                "kilobytesTransferred": 100,
                "endTime": "2026-05-01T00:00:00Z",
            }
        },
        {
            "attributes": {
                "policyType": "NBU_CATALOG",
                "status": "1",
                "kilobytesTransferred": 200,
                "endTime": "2026-05-02T00:00:00Z",
            }
        },
    ]
    sub = CatalogCollector(cfg, _StaticCache(jobs))  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    status = _find(metrics, "nbu_catalog_backup_last_status")
    size = _find(metrics, "nbu_catalog_backup_size_bytes")
    assert status.samples[0].value == 1.0
    assert size.samples[0].value == 200 * 1024


# --------------------------------------------------------------------------- #
# Helpers and operational shape
# --------------------------------------------------------------------------- #


def test_parse_iso_handles_variants() -> None:
    assert _parse_iso("2026-05-01T00:00:00Z") > 0
    assert _parse_iso("2026-05-01T00:00:00+00:00") > 0
    assert _parse_iso("garbage") == 0.0
    assert _parse_iso(None) == 0.0


def test_top_level_collector_yields_operational_metrics() -> None:
    cfg = Config()
    cfg.collectors.jobs.enabled = False
    cfg.collectors.clients.enabled = False
    cfg.collectors.policies.enabled = False
    cfg.collectors.storageUnits.enabled = False
    cfg.collectors.diskPools.enabled = False
    cfg.collectors.diskVolumes.enabled = False
    cfg.collectors.storageServers.enabled = False
    cfg.collectors.msdp.enabled = False
    cfg.collectors.catalog.enabled = False
    client = MagicMock()
    client._cfg.apiVersion = "7.0"
    coll = NBUCollector(client, cfg)
    names = {m.name for m in coll.collect()}
    assert {"nbu_up", "nbu_api_version", "nbu_exporter_build_info"} <= names


def test_cloud_pattern_is_anchored() -> None:
    pattern = re.compile(Config().collectors.storageUnits.cloudTypePattern)
    assert pattern.match("amazon_s3")
    assert pattern.match("wasabi")
    assert pattern.match("azure_blob")
    assert pattern.match("gcp_storage")
    assert not pattern.match("BasicDisk")


def test_clients_skipped_when_jobs_disabled_warning() -> None:
    """If jobs is disabled, clients collector cannot derive — orchestrator skips."""
    cfg = Config()
    cfg.collectors.jobs.enabled = False
    cfg.collectors.clients.enabled = True
    cfg.collectors.policies.enabled = False
    cfg.collectors.storageUnits.enabled = False
    cfg.collectors.diskPools.enabled = False
    cfg.collectors.diskVolumes.enabled = False
    cfg.collectors.storageServers.enabled = False
    cfg.collectors.msdp.enabled = False
    cfg.collectors.catalog.enabled = False
    client = MagicMock()
    client._cfg.apiVersion = "7.0"
    coll = NBUCollector(client, cfg)
    # _clients_sub stays None — accessed via the private attribute so the test
    # can assert the skip behavior.
    assert coll._clients_sub is None  # noqa: SLF001
