"""Tests for the Prometheus collectors."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

from prometheus_client.metrics_core import Metric

from nbu_exporter.collectors import (
    CatalogCollector,
    DiskPoolsCollector,
    JobsCollector,
    MSDPCollector,
    NBUCollector,
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
    # later attempt has status=1 — that should be the recorded last status
    sample = next(s for s in last_status.samples if s.labels["client"] == "host1")
    assert sample.value == 1.0


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
    sub = StorageUnitsCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))
    samples = _samples(_find(metrics, "nbu_disk_bytes"), "nbu_disk_bytes")
    sizes = {(labels["name"], labels["size"]) for labels, _ in samples}
    assert ("stu-aws", "used") in sizes
    assert ("stu-aws", "free") not in sizes  # cloud STU: free suppressed
    assert ("stu-disk", "free") in sizes


def test_disk_pool_up_state_mapping() -> None:
    cfg = Config()
    cfg.collectors.diskPools.upStateValues = [2]
    pools = [
        {
            "attributes": {
                "name": "pool-1",
                "state": 2,
                "totalCapacityBytes": 100,
                "usedCapacityBytes": 50,
                "storageServers": [{"name": "ss-1", "serverType": "PureDisk"}],
            }
        },
        {
            "attributes": {
                "name": "pool-2",
                "state": 4,
                "totalCapacityBytes": 100,
                "usedCapacityBytes": 25,
                "storageServers": [{"name": "ss-2", "serverType": "AdvancedDisk"}],
            }
        },
    ]
    client = MagicMock()
    client.get_all.return_value = pools
    sub = DiskPoolsCollector(client, cfg)
    metrics = list(sub.collect(MagicMock()))
    up = _find(metrics, "nbu_disk_pool_up")
    by_pool = {s.labels["pool"]: s.value for s in up.samples}
    assert by_pool["pool-1"] == 1.0
    assert by_pool["pool-2"] == 0.0
    ratio = _find(metrics, "nbu_disk_pool_used_ratio")
    by_pool_ratio = {s.labels["pool"]: s.value for s in ratio.samples}
    assert by_pool_ratio["pool-1"] == 0.5


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

    class FakeCache:
        def get(self) -> list[dict[str, Any]]:
            return pools

    sub = MSDPCollector(MagicMock(), cfg, FakeCache())  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    ratio = _find(metrics, "nbu_msdp_dedup_ratio")
    pools_emitted = {s.labels["pool"] for s in ratio.samples}
    assert pools_emitted == {"msdp-pool"}
    sample = next(s for s in ratio.samples if s.labels["pool"] == "msdp-pool")
    assert sample.value == 4.0  # 1000 / 250


def test_catalog_collector_picks_matching_policy_type() -> None:
    cfg = Config()
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
                "policyType": "NBU-Catalog",
                "status": "0",
                "kilobytesTransferred": 100,
                "endTime": "2026-05-01T00:00:00Z",
            }
        },
        {
            "attributes": {
                "policyType": "NBU-Catalog",
                "status": "1",
                "kilobytesTransferred": 200,
                "endTime": "2026-05-02T00:00:00Z",
            }
        },
    ]

    class FakeCache:
        def get(self) -> list[dict[str, Any]]:
            return jobs

    sub = CatalogCollector(cfg, FakeCache())  # type: ignore[arg-type]
    metrics = list(sub.collect(MagicMock()))
    status = _find(metrics, "nbu_catalog_backup_last_status")
    size = _find(metrics, "nbu_catalog_backup_size_bytes")
    assert status.samples[0].value == 1.0
    assert size.samples[0].value == 200 * 1024


def test_parse_iso_handles_variants() -> None:
    assert _parse_iso("2026-05-01T00:00:00Z") > 0
    assert _parse_iso("2026-05-01T00:00:00+00:00") > 0
    assert _parse_iso("garbage") == 0.0
    assert _parse_iso(None) == 0.0


def test_top_level_collector_yields_operational_metrics() -> None:
    cfg = Config()
    # Disable everything that hits the network — leave health-only.
    cfg.collectors.jobs.enabled = False
    cfg.collectors.clients.enabled = False
    cfg.collectors.policies.enabled = False
    cfg.collectors.storageUnits.enabled = False
    cfg.collectors.diskPools.enabled = False
    cfg.collectors.storageServers.enabled = False
    cfg.collectors.msdp.enabled = False
    cfg.collectors.catalog.enabled = False
    client = MagicMock()
    client._cfg.apiVersion = "3.0"
    coll = NBUCollector(client, cfg)
    names = {m.name for m in coll.collect()}
    assert {"nbu_up", "nbu_api_version", "nbu_exporter_build_info"} <= names


def test_cloud_pattern_is_anchored() -> None:
    """Sanity check: cfg.storageUnits.cloudTypePattern matches the expected prefixes."""
    pattern = re.compile(Config().collectors.storageUnits.cloudTypePattern)
    assert pattern.match("amazon_s3")
    assert pattern.match("wasabi")
    assert pattern.match("azure_blob")
    assert pattern.match("gcp_storage")
    assert not pattern.match("BasicDisk")
