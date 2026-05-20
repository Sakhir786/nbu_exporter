"""Prometheus collectors for the NetBackup REST API.

A single top-level :class:`NBUCollector` is registered with the default
registry. It owns a number of sub-collectors, each responsible for one
metric family group. Sub-collectors share a TTL-cached jobs list so the
expensive paginated /admin/jobs fetch happens once per scrape window.
"""

from __future__ import annotations

import logging
import platform
import re
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from prometheus_client.metrics_core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    Metric,
)

from nbu_exporter import __version__
from nbu_exporter.cache import TTLCache
from nbu_exporter.client import NBUClient
from nbu_exporter.config import CollectorsConfig

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_str(value: Any) -> str:
    """Coerce arbitrary JSON values into a label-safe string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_int(value: Any) -> int:
    """Coerce JSON values into an int, returning 0 on failure."""
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """Coerce JSON values into a float, returning 0.0 on failure."""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _job_attrs(job: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``attributes`` mapping from a JSON:API resource."""
    attrs = job.get("attributes")
    if isinstance(attrs, dict):
        return attrs
    # Some endpoints flatten attributes onto the top level — accept both shapes.
    return job


def _parse_iso(ts: Any) -> float:
    """Parse an ISO-8601 timestamp into Unix seconds; return 0.0 on failure."""
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


@dataclass
class ScrapeState:
    """Per-scrape mutable state shared across sub-collectors."""

    api_version: str
    successes: int = 0
    failures: int = 0
    durations: dict[str, float] = None  # type: ignore[assignment]
    error_totals: dict[str, int] = None  # type: ignore[assignment]
    last_scrape: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.durations is None:
            self.durations = {}
        if self.error_totals is None:
            self.error_totals = {}
        if self.last_scrape is None:
            self.last_scrape = {}


# --------------------------------------------------------------------------- #
# Jobs collector — the workhorse
# --------------------------------------------------------------------------- #


class JobsCollector:
    """Fetches /admin/jobs and emits job-aggregate metrics."""

    name = "jobs"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._jobs_cfg = cfg.jobs
        self._states_enabled = cfg.jobStates.enabled
        self._policy_jobs_enabled = cfg.policies.enabled
        self._client_jobs_enabled = cfg.clients.enabled
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.jobs.cacheTTLSeconds,
            fetcher=self._fetch_jobs,
        )

    def _fetch_jobs(self) -> list[dict[str, Any]]:
        lookback_start = datetime.now(timezone.utc) - timedelta(hours=self._jobs_cfg.lookbackHours)
        iso = lookback_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"filter": f"startTime ge {iso}", "sort": "-startTime"}
        started = time.monotonic()
        jobs = self._client.get_all(
            "/admin/jobs",
            params=params,
            page_size=self._jobs_cfg.pageSize,
            max_pages=self._jobs_cfg.maxPages,
        )
        LOGGER.info(
            "jobs collector: fetched %d jobs in %.2fs (lookback=%dh)",
            len(jobs),
            time.monotonic() - started,
            self._jobs_cfg.lookbackHours,
        )
        return jobs

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        jobs = self.cache.get()

        nbu_jobs_count = GaugeMetricFamily(
            "nbu_jobs_count",
            "NetBackup job count, dimensioned by action, policy_type and status.",
            labels=["action", "policy_type", "status"],
        )
        nbu_jobs_bytes = GaugeMetricFamily(
            "nbu_jobs_bytes",
            "NetBackup bytes processed by jobs, dimensioned by action, policy_type and status.",
            labels=["action", "policy_type", "status"],
        )
        nbu_status_count = GaugeMetricFamily(
            "nbu_status_count",
            "NetBackup job count rolled up by action and status only.",
            labels=["action", "status"],
        )
        nbu_jobs_by_state = GaugeMetricFamily(
            "nbu_jobs_by_state",
            "Job count grouped by NBU state, action and policy_type.",
            labels=["state", "action", "policy_type"],
        )
        nbu_policy_jobs_count = GaugeMetricFamily(
            "nbu_policy_jobs_count",
            "Jobs grouped by policy name.",
            labels=["policy_name", "policy_type", "action", "status"],
        )
        nbu_policy_jobs_bytes = GaugeMetricFamily(
            "nbu_policy_jobs_bytes",
            "Bytes processed grouped by policy name.",
            labels=["policy_name", "policy_type", "action", "status"],
        )
        nbu_client_last_success = GaugeMetricFamily(
            "nbu_client_last_successful_backup_timestamp",
            "Unix timestamp of the most recent successful backup per client/policy/schedule.",
            labels=["client", "policy", "schedule"],
        )
        nbu_client_last_status = GaugeMetricFamily(
            "nbu_client_last_attempt_status",
            "Exit code of the most recent backup attempt per client/policy/schedule.",
            labels=["client", "policy", "schedule"],
        )

        count_by_aps: dict[tuple[str, str, str], int] = {}
        bytes_by_aps: dict[tuple[str, str, str], int] = {}
        count_by_as: dict[tuple[str, str], int] = {}
        count_by_state: dict[tuple[str, str, str], int] = {}
        policy_count: dict[tuple[str, str, str, str], int] = {}
        policy_bytes: dict[tuple[str, str, str, str], int] = {}
        last_success: dict[tuple[str, str, str], tuple[float, int]] = {}
        last_attempt: dict[tuple[str, str, str], tuple[float, int]] = {}

        for job in jobs:
            attrs = _job_attrs(job)
            action = _as_str(attrs.get("jobType")) or _as_str(attrs.get("type"))
            policy_type = _as_str(attrs.get("policyType"))
            status = _as_str(attrs.get("status"))
            state = _as_str(attrs.get("state"))
            policy_name = _as_str(attrs.get("policyName"))
            client_name = _as_str(attrs.get("clientName"))
            schedule_name = _as_str(attrs.get("scheduleName"))
            kbytes = _as_int(attrs.get("kilobytesTransferred"))
            byte_count = kbytes * 1024
            end_time = _parse_iso(attrs.get("endTime")) or _parse_iso(attrs.get("startTime"))

            aps = (action, policy_type, status)
            count_by_aps[aps] = count_by_aps.get(aps, 0) + 1
            bytes_by_aps[aps] = bytes_by_aps.get(aps, 0) + byte_count

            asx = (action, status)
            count_by_as[asx] = count_by_as.get(asx, 0) + 1

            if self._states_enabled and state:
                sx = (state, action, policy_type)
                count_by_state[sx] = count_by_state.get(sx, 0) + 1

            if self._policy_jobs_enabled and policy_name:
                pk = (policy_name, policy_type, action, status)
                policy_count[pk] = policy_count.get(pk, 0) + 1
                policy_bytes[pk] = policy_bytes.get(pk, 0) + byte_count

            if self._client_jobs_enabled and client_name:
                ck = (client_name, policy_name, schedule_name)
                status_int = _as_int(status)
                prev_attempt = last_attempt.get(ck)
                if prev_attempt is None or end_time > prev_attempt[0]:
                    last_attempt[ck] = (end_time, status_int)
                if status_int == 0:
                    prev_success = last_success.get(ck)
                    if prev_success is None or end_time > prev_success[0]:
                        last_success[ck] = (end_time, status_int)

        for (action, policy_type, status), count in count_by_aps.items():
            nbu_jobs_count.add_metric([action, policy_type, status], count)
            nbu_jobs_bytes.add_metric(
                [action, policy_type, status], bytes_by_aps.get((action, policy_type, status), 0)
            )
        for (action, status), count in count_by_as.items():
            nbu_status_count.add_metric([action, status], count)

        yield nbu_jobs_count
        yield nbu_jobs_bytes
        yield nbu_status_count

        if self._states_enabled:
            for (state, action, policy_type), count in count_by_state.items():
                nbu_jobs_by_state.add_metric([state, action, policy_type], count)
            yield nbu_jobs_by_state

        if self._policy_jobs_enabled:
            for (policy_name, policy_type, action, status), count in policy_count.items():
                nbu_policy_jobs_count.add_metric([policy_name, policy_type, action, status], count)
                nbu_policy_jobs_bytes.add_metric(
                    [policy_name, policy_type, action, status],
                    policy_bytes.get((policy_name, policy_type, action, status), 0),
                )
            yield nbu_policy_jobs_count
            yield nbu_policy_jobs_bytes

        if self._client_jobs_enabled:
            for (client_name, policy, schedule), (ts, _status) in last_success.items():
                nbu_client_last_success.add_metric([client_name, policy, schedule], ts)
            for (client_name, policy, schedule), (_ts, status_int) in last_attempt.items():
                nbu_client_last_status.add_metric(
                    [client_name, policy, schedule], float(status_int)
                )
            yield nbu_client_last_success
            yield nbu_client_last_status


# --------------------------------------------------------------------------- #
# Clients collector — inventory only
# --------------------------------------------------------------------------- #


class ClientsCollector:
    name = "clients"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg.clients
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.clients.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        # Page size and cap reuse the jobs collector limits to stay bounded.
        return self._client.get_all(
            "/config/hosts",
            params={"filter": "hostType eq 'CLIENT'"},
            page_size=500,
            max_pages=200,
        )

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        clients = self.cache.get()
        info = GaugeMetricFamily(
            "nbu_client_info",
            "NetBackup client inventory; constant 1, labels carry metadata.",
            labels=["client", "hardware", "os"],
        )
        for entry in clients:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name") or attrs.get("hostName"))
            if not name:
                continue
            hardware = _as_str(attrs.get("hardware") or attrs.get("hardwareType"))
            os_name = _as_str(attrs.get("os") or attrs.get("osType"))
            info.add_metric([name, hardware, os_name], 1.0)
        yield info


# --------------------------------------------------------------------------- #
# Policies collector
# --------------------------------------------------------------------------- #


class PoliciesCollector:
    name = "policies"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg.policies
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.policies.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/config/policies",
            params=None,
            page_size=500,
            max_pages=200,
        )

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        policies = self.cache.get()
        info = GaugeMetricFamily(
            "nbu_policy_info",
            "NetBackup policy inventory; constant 1, labels carry metadata.",
            labels=["policy_name", "policy_type", "active"],
        )
        for entry in policies:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("policyName") or attrs.get("name"))
            if not name:
                continue
            ptype = _as_str(attrs.get("policyType") or attrs.get("type"))
            active = _as_str(attrs.get("active", attrs.get("isActive", False)))
            info.add_metric([name, ptype, active], 1.0)
        yield info


# --------------------------------------------------------------------------- #
# Storage units collector
# --------------------------------------------------------------------------- #


class StorageUnitsCollector:
    name = "storage_units"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg.storageUnits
        self._cloud_re = re.compile(cfg.storageUnits.cloudTypePattern)
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.storageUnits.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/storage/storage-units",
            params=None,
            page_size=500,
            max_pages=200,
        )

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        units = self.cache.get()
        disk_bytes = GaugeMetricFamily(
            "nbu_disk_bytes",
            "Storage unit capacity in bytes; size label is free or used.",
            labels=["name", "type", "size"],
        )
        for entry in units:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name"))
            if not name:
                continue
            stype = _as_str(attrs.get("storageUnitType") or attrs.get("type"))
            used = _as_float(attrs.get("usedBytes"))
            free = _as_float(attrs.get("freeBytes"))
            is_cloud = bool(self._cloud_re.match(stype.lower())) if stype else False
            disk_bytes.add_metric([name, stype, "used"], used)
            if not is_cloud:
                disk_bytes.add_metric([name, stype, "free"], free)
        yield disk_bytes


# --------------------------------------------------------------------------- #
# Disk pools collector
# --------------------------------------------------------------------------- #


class DiskPoolsCollector:
    name = "disk_pools"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg.diskPools
        self._up_states = set(cfg.diskPools.upStateValues)
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.diskPools.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/storage/disk-pools",
            params=None,
            page_size=500,
            max_pages=200,
        )

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        pools = self.cache.get()
        cap = GaugeMetricFamily(
            "nbu_disk_pool_capacity_bytes",
            "Disk pool total capacity in bytes.",
            labels=["pool", "storage_server", "server_type"],
        )
        used = GaugeMetricFamily(
            "nbu_disk_pool_used_bytes",
            "Disk pool used bytes.",
            labels=["pool", "storage_server", "server_type"],
        )
        ratio = GaugeMetricFamily(
            "nbu_disk_pool_used_ratio",
            "Disk pool used / capacity, 0.0 to 1.0.",
            labels=["pool", "storage_server", "server_type"],
        )
        state = GaugeMetricFamily(
            "nbu_disk_pool_state",
            "Raw numeric state code reported by the NBU API.",
            labels=["pool", "storage_server", "server_type"],
        )
        up = GaugeMetricFamily(
            "nbu_disk_pool_up",
            "1 if disk pool state is in collectors.diskPools.upStateValues, else 0.",
            labels=["pool"],
        )
        volumes = GaugeMetricFamily(
            "nbu_disk_pool_volumes",
            "Number of volumes in the disk pool.",
            labels=["pool"],
        )
        for entry in pools:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name") or attrs.get("diskPoolName"))
            if not name:
                continue
            servers = attrs.get("storageServers") or []
            if isinstance(servers, list) and servers:
                first = servers[0]
                if isinstance(first, dict):
                    server_name = _as_str(first.get("name") or first.get("hostName"))
                    server_type = _as_str(first.get("serverType") or first.get("type"))
                else:
                    server_name = _as_str(first)
                    server_type = ""
            else:
                server_name = _as_str(attrs.get("storageServerName"))
                server_type = _as_str(attrs.get("storageServerType"))
            capacity_bytes = _as_float(attrs.get("totalCapacityBytes") or attrs.get("capacity"))
            used_bytes = _as_float(attrs.get("usedCapacityBytes") or attrs.get("used"))
            state_val = _as_int(attrs.get("state"))
            vol_count = _as_int(attrs.get("numVolumes") or attrs.get("volumeCount"))
            cap.add_metric([name, server_name, server_type], capacity_bytes)
            used.add_metric([name, server_name, server_type], used_bytes)
            if capacity_bytes > 0:
                ratio.add_metric([name, server_name, server_type], used_bytes / capacity_bytes)
            state.add_metric([name, server_name, server_type], float(state_val))
            up.add_metric([name], 1.0 if state_val in self._up_states else 0.0)
            volumes.add_metric([name], float(vol_count))
        yield cap
        yield used
        yield ratio
        yield state
        yield up
        yield volumes


# --------------------------------------------------------------------------- #
# Storage servers collector
# --------------------------------------------------------------------------- #


class StorageServersCollector:
    name = "storage_servers"

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg.storageServers
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.storageServers.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/storage/storage-servers",
            params=None,
            page_size=500,
            max_pages=200,
        )

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        servers = self.cache.get()
        info = GaugeMetricFamily(
            "nbu_storage_server_info",
            "Storage server inventory; constant 1, labels carry metadata.",
            labels=["name", "type", "category"],
        )
        state = GaugeMetricFamily(
            "nbu_storage_server_state",
            "Raw numeric state code reported by the NBU API.",
            labels=["name", "type"],
        )
        for entry in servers:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name") or attrs.get("hostName"))
            if not name:
                continue
            stype = _as_str(attrs.get("serverType") or attrs.get("type"))
            category = _as_str(attrs.get("category") or attrs.get("storageCategory"))
            state_val = _as_int(attrs.get("state"))
            info.add_metric([name, stype, category], 1.0)
            state.add_metric([name, stype], float(state_val))
        yield info
        yield state


# --------------------------------------------------------------------------- #
# MSDP collector
# --------------------------------------------------------------------------- #


class MSDPCollector:
    name = "msdp"

    def __init__(
        self,
        client: NBUClient,
        cfg: CollectorsConfig,
        disk_pools_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._client = client
        self._cfg = cfg.msdp
        self._pattern = re.compile(cfg.msdp.serverTypePattern)
        self._disk_pools_cache = disk_pools_cache

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        pools = self._disk_pools_cache.get()
        dedup = GaugeMetricFamily(
            "nbu_msdp_dedup_ratio",
            "MSDP deduplication ratio as a decimal (logical / physical).",
            labels=["pool", "storage_server"],
        )
        logical = GaugeMetricFamily(
            "nbu_msdp_logical_bytes",
            "MSDP pre-dedup (logical) bytes.",
            labels=["pool", "storage_server"],
        )
        physical = GaugeMetricFamily(
            "nbu_msdp_physical_bytes",
            "MSDP post-dedup (physical) bytes.",
            labels=["pool", "storage_server"],
        )
        for entry in pools:
            attrs = _job_attrs(entry)
            servers = attrs.get("storageServers") or []
            server_type = ""
            server_name = ""
            if isinstance(servers, list) and servers:
                first = servers[0]
                if isinstance(first, dict):
                    server_type = _as_str(first.get("serverType") or first.get("type"))
                    server_name = _as_str(first.get("name") or first.get("hostName"))
            if not server_type:
                server_type = _as_str(attrs.get("storageServerType"))
                server_name = _as_str(attrs.get("storageServerName"))
            if not server_type or not self._pattern.search(server_type):
                continue
            name = _as_str(attrs.get("name") or attrs.get("diskPoolName"))
            if not name:
                continue
            logical_b = _as_float(attrs.get("logicalBytes") or attrs.get("preDedupBytes"))
            physical_b = _as_float(
                attrs.get("physicalBytes")
                or attrs.get("postDedupBytes")
                or attrs.get("usedCapacityBytes")
            )
            ratio_v = 0.0
            if physical_b > 0:
                ratio_v = logical_b / physical_b
            elif attrs.get("dedupRatio") is not None:
                ratio_v = _as_float(attrs.get("dedupRatio"))
            dedup.add_metric([name, server_name], ratio_v)
            logical.add_metric([name, server_name], logical_b)
            physical.add_metric([name, server_name], physical_b)
        yield dedup
        yield logical
        yield physical


# --------------------------------------------------------------------------- #
# Catalog backup collector — derived from the shared jobs cache
# --------------------------------------------------------------------------- #


class CatalogCollector:
    name = "catalog"

    def __init__(
        self,
        cfg: CollectorsConfig,
        jobs_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._cfg = cfg.catalog
        self._types = {t.lower() for t in cfg.catalog.policyTypeValues}
        self._jobs_cache = jobs_cache

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        jobs = self._jobs_cache.get()
        last_ts = 0.0
        last_status = 0
        last_size = 0
        for job in jobs:
            attrs = _job_attrs(job)
            ptype = _as_str(attrs.get("policyType")).lower()
            if ptype not in self._types:
                continue
            ts = _parse_iso(attrs.get("endTime")) or _parse_iso(attrs.get("startTime"))
            if ts > last_ts:
                last_ts = ts
                last_status = _as_int(attrs.get("status"))
                last_size = _as_int(attrs.get("kilobytesTransferred")) * 1024

        ts_m = GaugeMetricFamily(
            "nbu_catalog_backup_last_timestamp",
            "Unix timestamp of the last catalog backup job seen in the lookback window.",
        )
        st_m = GaugeMetricFamily(
            "nbu_catalog_backup_last_status",
            "Exit status of the last catalog backup job seen.",
        )
        sz_m = GaugeMetricFamily(
            "nbu_catalog_backup_size_bytes",
            "Bytes transferred by the last catalog backup job seen.",
        )
        ts_m.add_metric([], last_ts)
        st_m.add_metric([], float(last_status))
        sz_m.add_metric([], float(last_size))
        yield ts_m
        yield st_m
        yield sz_m


# --------------------------------------------------------------------------- #
# Top-level orchestrator
# --------------------------------------------------------------------------- #


class NBUCollector:
    """Top-level Prometheus collector that orchestrates all sub-collectors."""

    def __init__(self, client: NBUClient, cfg: CollectorsConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._lock = threading.Lock()
        self._error_totals: dict[str, int] = {}

        self._jobs_sub = JobsCollector(client, cfg) if cfg.jobs.enabled else None
        self._clients_sub = ClientsCollector(client, cfg) if cfg.clients.enabled else None
        self._policies_sub = PoliciesCollector(client, cfg) if cfg.policies.enabled else None
        self._storage_units_sub = (
            StorageUnitsCollector(client, cfg) if cfg.storageUnits.enabled else None
        )
        self._disk_pools_sub = DiskPoolsCollector(client, cfg) if cfg.diskPools.enabled else None
        self._storage_servers_sub = (
            StorageServersCollector(client, cfg) if cfg.storageServers.enabled else None
        )
        self._msdp_sub = None
        if cfg.msdp.enabled and self._disk_pools_sub is not None:
            self._msdp_sub = MSDPCollector(client, cfg, self._disk_pools_sub.cache)
        self._catalog_sub = None
        if cfg.catalog.enabled and self._jobs_sub is not None:
            self._catalog_sub = CatalogCollector(cfg, self._jobs_sub.cache)

    def _enabled_map(self) -> dict[str, bool]:
        return {
            "jobs": self._cfg.jobs.enabled,
            "clients": self._cfg.clients.enabled,
            "policies": self._cfg.policies.enabled,
            "storage_units": self._cfg.storageUnits.enabled,
            "disk_pools": self._cfg.diskPools.enabled,
            "storage_servers": self._cfg.storageServers.enabled,
            "msdp": self._cfg.msdp.enabled,
            "catalog": self._cfg.catalog.enabled,
            "job_states": self._cfg.jobStates.enabled,
            "health": self._cfg.health.enabled,
        }

    def _run_sub(
        self,
        sub: Any,
        state: ScrapeState,
        collected: list[Metric],
    ) -> None:
        name = sub.name
        started = time.monotonic()
        try:
            for metric in sub.collect(state):
                collected.append(metric)
            state.successes += 1
            state.last_scrape[name] = time.time()
        except Exception as exc:  # noqa: BLE001 — wrap to keep scrape alive
            LOGGER.error("collector %s failed: %s", name, exc, exc_info=True)
            state.failures += 1
            with self._lock:
                self._error_totals[name] = self._error_totals.get(name, 0) + 1
        finally:
            state.durations[name] = time.monotonic() - started

    def collect(self) -> Iterable[Metric]:
        state = ScrapeState(api_version=self._client._cfg.apiVersion)  # noqa: SLF001
        collected: list[Metric] = []

        for sub in (
            self._jobs_sub,
            self._clients_sub,
            self._policies_sub,
            self._storage_units_sub,
            self._disk_pools_sub,
            self._storage_servers_sub,
            self._msdp_sub,
            self._catalog_sub,
        ):
            if sub is None:
                continue
            self._run_sub(sub, state, collected)

        yield from collected

        # Exporter-operational metric families
        build_info = GaugeMetricFamily(
            "nbu_exporter_build_info",
            "Exporter build information; constant 1.",
            labels=["version", "python_version"],
        )
        build_info.add_metric([__version__, platform.python_version()], 1.0)
        yield build_info

        enabled = GaugeMetricFamily(
            "nbu_exporter_collector_enabled",
            "1 if the collector is enabled in config, else 0.",
            labels=["collector"],
        )
        for name, on in self._enabled_map().items():
            enabled.add_metric([name], 1.0 if on else 0.0)
        yield enabled

        durations = GaugeMetricFamily(
            "nbu_exporter_scrape_duration_seconds",
            "Last scrape duration per collector.",
            labels=["collector"],
        )
        for name, dur in state.durations.items():
            durations.add_metric([name], dur)
        yield durations

        errors = CounterMetricFamily(
            "nbu_exporter_scrape_errors_total",
            "Cumulative scrape errors per collector since exporter start.",
            labels=["collector"],
        )
        with self._lock:
            snapshot = dict(self._error_totals)
        for name, count in snapshot.items():
            errors.add_metric([name], float(count))
        yield errors

        last_scrape = GaugeMetricFamily(
            "nbu_last_scrape_timestamp_seconds",
            "Unix seconds of the last successful scrape per collector source.",
            labels=["source"],
        )
        for name, ts in state.last_scrape.items():
            last_scrape.add_metric([name], ts)
        yield last_scrape

        api_version = GaugeMetricFamily(
            "nbu_api_version",
            "NetBackup REST API version negotiated for this scrape; constant 1.",
            labels=["version"],
        )
        api_version.add_metric([state.api_version], 1.0)
        yield api_version

        up = GaugeMetricFamily(
            "nbu_up",
            "1 if at least one collector succeeded this scrape, else 0.",
        )
        up.add_metric([], 1.0 if state.successes > 0 else 0.0)
        yield up


def python_version() -> str:
    """Return the running Python interpreter version (helper for tests)."""
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
