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
from nbu_exporter.config import Config

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

    def __init__(self, client: NBUClient, cfg: Config) -> None:
        self._client = client
        self._jobs_cfg = cfg.collectors.jobs
        self._states_enabled = cfg.collectors.jobStates.enabled
        self._policy_jobs_enabled = cfg.collectors.policies.enabled
        self._client_jobs_enabled = cfg.collectors.clients.enabled
        self._page_size, self._max_pages, self._style = cfg.resolve_pagination(
            cfg.collectors.jobs.pageSize, cfg.collectors.jobs.maxPages
        )
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.collectors.jobs.cacheTTLSeconds,
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
            page_size=self._page_size,
            max_pages=self._max_pages,
            style=self._style,
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
    """Derive the client inventory from cached job records.

    NBU 10.0 returns HTTP 404 from /config/hosts, /config/clients and
    /admin/clients. The only reliable source for "what clients does this
    master see" is the clientName field on each job. We dedup and emit.
    The old hostsEndpoint / hostTypeField / clientHostTypeValues knobs in
    ``ClientsCollectorConfig`` are retained so v0.3 configs keep loading
    cleanly; they are unused here.
    """

    name = "clients"

    def __init__(
        self,
        client: NBUClient,
        cfg: Config,
        jobs_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._client = client
        self._cfg = cfg.collectors.clients
        self._jobs_cache = jobs_cache

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        jobs = self._jobs_cache.get()
        info = GaugeMetricFamily(
            "nbu_client_info",
            "NetBackup client inventory derived from job records; constant 1.",
            labels=["client", "hardware", "os"],
        )
        seen: set[str] = set()
        for job in jobs:
            attrs = _job_attrs(job)
            name = _as_str(attrs.get("clientName")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            # hardware/os are not present on /admin/jobs records.
            info.add_metric([name, "", ""], 1.0)
        yield info


# --------------------------------------------------------------------------- #
# Policies collector
# --------------------------------------------------------------------------- #


class PoliciesCollector:
    """Derive the policy inventory from cached job records.

    Same rationale as ClientsCollector: /config/policies is unreliable on
    NBU 10.0. The jobs cache gives us a complete view of every policy that
    has run inside the lookback window.
    """

    name = "policies"

    def __init__(
        self,
        client: NBUClient,
        cfg: Config,
        jobs_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._client = client
        self._cfg = cfg.collectors.policies
        self._jobs_cache = jobs_cache

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        jobs = self._jobs_cache.get()
        info = GaugeMetricFamily(
            "nbu_policy_info",
            "NetBackup policy inventory derived from job records; constant 1.",
            labels=["policy_name", "policy_type", "active"],
        )
        seen: set[tuple[str, str]] = set()
        for job in jobs:
            attrs = _job_attrs(job)
            name = _as_str(attrs.get("policyName")).strip()
            if not name:
                continue
            ptype = _as_str(attrs.get("policyType")).strip()
            key = (name, ptype)
            if key in seen:
                continue
            seen.add(key)
            info.add_metric([name, ptype, ""], 1.0)
        yield info


# --------------------------------------------------------------------------- #
# Storage units collector
# --------------------------------------------------------------------------- #


class StorageUnitsCollector:
    """Emit storage-unit capacity and supplement labels from cached jobs.

    NBU 10.0 returns ``null`` for ``mediaServer`` and ``storageUnitType``
    on /storage/storage-units. We back-fill those labels by inspecting
    job records that targeted each storage unit.
    """

    name = "storage_units"

    def __init__(
        self,
        client: NBUClient,
        cfg: Config,
        jobs_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._client = client
        self._cfg = cfg.collectors.storageUnits
        self._cloud_re = re.compile(cfg.collectors.storageUnits.cloudTypePattern)
        self._page_size, self._max_pages, self._style = cfg.resolve_pagination(
            cfg.collectors.storageUnits.pageSize, cfg.collectors.storageUnits.maxPages
        )
        self._jobs_cache = jobs_cache
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.collectors.storageUnits.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/storage/storage-units",
            params=None,
            page_size=self._page_size,
            max_pages=self._max_pages,
            style=self._style,
        )

    def _supplement_from_jobs(self) -> dict[str, tuple[str, str]]:
        """Build {storage_unit -> (media_server, type)} from cached job records."""
        out: dict[str, tuple[str, str]] = {}
        for job in self._jobs_cache.get():
            attrs = _job_attrs(job)
            su = _as_str(attrs.get("destinationStorageUnitName")).strip()
            if not su:
                continue
            ms = _as_str(attrs.get("destinationMediaServerName")).strip()
            # First sighting wins — the field is consistent per storage unit.
            out.setdefault(su, (ms, ""))
        return out

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        units = self.cache.get()
        job_meta = self._supplement_from_jobs()
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
            if not stype:
                # Back-fill from jobs (type field not present there yet,
                # but the lookup keeps the contract symmetric).
                stype = job_meta.get(name, ("", ""))[1]
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
    """Read /storage/disk-pools with fields=*&include=* and merge per-pool detail.

    Calling /storage/disk-pools without the ``fields=*&include=*`` query
    params yields nulls for capacity on NBU 10.0. We always pass them, then
    for each non-cloud pool fetch /storage/disk-pools/{sType:Name} to pick
    up the pool-level aggregates (rawSizeBytes, usableSizeBytes,
    availableSpaceBytes, usedCapacityBytes) that the list endpoint omits.
    State is reported as the string ``"UP"`` / ``"DOWN"`` — parse it as
    such; the old numeric upStateValues from v0.3 is ignored but kept on
    the config dataclass so existing config files still load.
    """

    name = "disk_pools"

    def __init__(self, client: NBUClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg.collectors.diskPools
        self._page_size, self._max_pages, self._style = cfg.resolve_pagination(
            cfg.collectors.diskPools.pageSize, cfg.collectors.diskPools.maxPages
        )
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.collectors.diskPools.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        pools = self._client.get_all(
            "/storage/disk-pools",
            params={"fields": "*", "include": "*"},
            page_size=self._page_size,
            max_pages=self._max_pages,
            style=self._style,
        )
        for pool in pools:
            attrs = _job_attrs(pool)
            category = _as_str(attrs.get("storageCategory"))
            if category == "CLOUD":
                # Cloud pools report nothing useful at the detail endpoint.
                continue
            pool_id = _as_str(pool.get("id"))
            if not pool_id:
                continue
            try:
                detail = self._client.get(
                    f"/storage/disk-pools/{pool_id}", params={"fields": "*"}
                )
            except Exception as exc:  # noqa: BLE001 — per-pool failure must not abort scrape
                LOGGER.warning("disk pool detail fetch failed for %s: %s", pool_id, exc)
                continue
            data = detail.get("data") if isinstance(detail, dict) else None
            detail_attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
            if not isinstance(detail_attrs, dict):
                continue
            for k in ("rawSizeBytes", "usableSizeBytes", "availableSpaceBytes", "usedCapacityBytes"):
                if k in detail_attrs:
                    attrs[k] = detail_attrs[k]
        return pools

    @staticmethod
    def _storage_server_label(entry: dict[str, Any]) -> str:
        """Return the storage server name from JSON:API relationships."""
        rels = entry.get("relationships")
        if not isinstance(rels, dict):
            return ""
        ss = rels.get("storageServers")
        if not isinstance(ss, dict):
            return ""
        data = ss.get("data")
        if not isinstance(data, list) or not data:
            return ""
        first = data[0]
        if not isinstance(first, dict):
            return ""
        sid = _as_str(first.get("id"))
        # id format is "sType:name" on NBU 10.0; split once if present.
        return sid.split(":", 1)[1] if ":" in sid else sid

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        pools = self.cache.get()
        cap = GaugeMetricFamily(
            "nbu_disk_pool_capacity_bytes",
            "Disk pool total raw capacity in bytes.",
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
            "1 if the pool's diskPoolState is UP, else 0.",
            labels=["pool", "storage_server", "server_type"],
        )
        up = GaugeMetricFamily(
            "nbu_disk_pool_up",
            "1 if the pool's diskPoolState is UP, else 0.",
            labels=["pool"],
        )
        volumes = GaugeMetricFamily(
            "nbu_disk_pool_volumes",
            "Number of volumes in the disk pool.",
            labels=["pool"],
        )
        high_wm = GaugeMetricFamily(
            "nbu_disk_pool_high_watermark_percent",
            "Configured high watermark percent for the pool.",
            labels=["pool"],
        )
        low_wm = GaugeMetricFamily(
            "nbu_disk_pool_low_watermark_percent",
            "Configured low watermark percent for the pool.",
            labels=["pool"],
        )
        capabilities = GaugeMetricFamily(
            "nbu_disk_pool_capability",
            "Disk pool capability strings reported by NBU; constant 1 per capability.",
            labels=["pool", "capability"],
        )

        for entry in pools:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name") or attrs.get("diskPoolName"))
            if not name:
                continue
            stype = _as_str(attrs.get("sType") or attrs.get("storageServerType"))
            server_name = self._storage_server_label(entry)
            capacity_bytes = _as_float(
                attrs.get("rawSizeBytes")
                or attrs.get("totalCapacityBytes")
                or attrs.get("capacity")
            )
            used_bytes = _as_float(attrs.get("usedCapacityBytes") or attrs.get("used"))
            state_str = _as_str(attrs.get("diskPoolState") or attrs.get("state"))
            is_up = 1.0 if state_str.upper() == "UP" else 0.0
            vol_list = attrs.get("diskVolumes") or attrs.get("volumes") or []
            vol_count = (
                len(vol_list)
                if isinstance(vol_list, list)
                else _as_int(attrs.get("numVolumes") or attrs.get("volumeCount"))
            )

            cap.add_metric([name, server_name, stype], capacity_bytes)
            used.add_metric([name, server_name, stype], used_bytes)
            if capacity_bytes > 0:
                ratio.add_metric([name, server_name, stype], used_bytes / capacity_bytes)
            state.add_metric([name, server_name, stype], is_up)
            up.add_metric([name], is_up)
            volumes.add_metric([name], float(vol_count))

            for k, metric in (("highWaterMark", high_wm), ("lowWaterMark", low_wm)):
                v = attrs.get(k)
                if v is not None:
                    metric.add_metric([name], _as_float(v))

            caps = attrs.get("diskPoolCapabilities")
            if isinstance(caps, list):
                for c in caps:
                    capabilities.add_metric([name, _as_str(c)], 1.0)

        yield cap
        yield used
        yield ratio
        yield state
        yield up
        yield volumes
        yield high_wm
        yield low_wm
        yield capabilities


# --------------------------------------------------------------------------- #
# Storage servers collector
# --------------------------------------------------------------------------- #


class StorageServersCollector:
    """Read /storage/storage-servers with fields=*&include=*.

    NBU 10.0's ``storageServerState`` is a string ("UP" / "DOWN"). The
    previous v0.3 code parsed it as int and always emitted 0; with the
    string parser here, plus the ``fields=*&include=*`` query parameters,
    the existing ``nbu_storage_server_state`` and ``nbu_storage_server_info``
    metrics start returning real values.
    """

    name = "storage_servers"

    def __init__(self, client: NBUClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg.collectors.storageServers
        self._page_size, self._max_pages, self._style = cfg.resolve_pagination(
            cfg.collectors.storageServers.pageSize, cfg.collectors.storageServers.maxPages
        )
        self.cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=cfg.collectors.storageServers.cacheTTLSeconds,
            fetcher=self._fetch,
        )

    def _fetch(self) -> list[dict[str, Any]]:
        return self._client.get_all(
            "/storage/storage-servers",
            params={"fields": "*", "include": "*"},
            page_size=self._page_size,
            max_pages=self._max_pages,
            style=self._style,
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
            "1 if storageServerState is UP, else 0.",
            labels=["name", "type"],
        )
        status_code = GaugeMetricFamily(
            "nbu_storage_server_nbu_status_code",
            "Raw nbuStatusCode reported by the storage server (omitted when null).",
            labels=["name", "type"],
        )
        version = GaugeMetricFamily(
            "nbu_storage_server_version",
            "Storage server nbuHostVersion; constant 1, version label carries the value.",
            labels=["name", "version"],
        )
        capability = GaugeMetricFamily(
            "nbu_storage_server_capability",
            "Storage server capability strings; constant 1 per capability.",
            labels=["name", "capability"],
        )

        for entry in servers:
            attrs = _job_attrs(entry)
            name = _as_str(attrs.get("name") or attrs.get("hostName"))
            if not name:
                continue
            stype = _as_str(attrs.get("sType") or attrs.get("serverType") or attrs.get("type"))
            category = _as_str(attrs.get("storageCategory") or attrs.get("category"))
            state_str = _as_str(attrs.get("storageServerState") or attrs.get("state"))
            is_up = 1.0 if state_str.upper() == "UP" else 0.0

            info.add_metric([name, stype, category], 1.0)
            state.add_metric([name, stype], is_up)

            raw_status = attrs.get("nbuStatusCode")
            if raw_status is not None:
                status_code.add_metric([name, stype], _as_float(raw_status))

            ver = _as_str(attrs.get("nbuHostVersion"))
            if ver:
                version.add_metric([name, ver], 1.0)

            caps = attrs.get("storageServerCapabilities")
            if isinstance(caps, list):
                for c in caps:
                    capability.add_metric([name, _as_str(c)], 1.0)

        yield info
        yield state
        yield status_code
        yield version
        yield capability


# --------------------------------------------------------------------------- #
# MSDP collector
# --------------------------------------------------------------------------- #


class MSDPCollector:
    """MSDP dedup ratio / logical / physical bytes (best-effort).

    NBU 10.0 REST does not expose ``logicalBytes`` / ``physicalBytes`` /
    ``dedupRatio`` on any tested endpoint. The metrics keep emitting (so
    existing dashboards do not break) but values are 0 until the master
    surfaces them. True dedup ratio requires ``crcontrol --dsstat`` on the
    MSDP node, which the exporter cannot reach over REST.
    """

    name = "msdp"

    def __init__(
        self,
        client: NBUClient,
        cfg: Config,
        disk_pools_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._client = client
        self._cfg = cfg.collectors.msdp
        self._pattern = re.compile(cfg.collectors.msdp.serverTypePattern)
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
# Disk volumes collector — derived from the shared disk-pools cache
# --------------------------------------------------------------------------- #


class DiskVolumesCollector:
    """Per-volume runtime metrics extracted from the diskVolumes[] arrays.

    Same data shared by ``DiskPoolsCollector`` — this collector makes no
    additional API call. It surfaces the per-volume UP/DOWN state, which is
    the only place we can see when a single volume on an otherwise-healthy
    pool is unreachable from the master.
    """

    name = "disk_volumes"

    def __init__(
        self,
        cfg: Config,
        disk_pools_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._cfg = cfg.collectors.diskVolumes
        self._disk_pools_cache = disk_pools_cache

    def collect(self, _state: ScrapeState) -> Iterable[Metric]:
        pools = self._disk_pools_cache.get()
        up = GaugeMetricFamily(
            "nbu_disk_volume_up",
            "1 if the volume's state is UP, else 0.",
            labels=["pool", "volume", "media_id"],
        )
        cap = GaugeMetricFamily(
            "nbu_disk_volume_capacity_bytes",
            "Volume total capacity in bytes.",
            labels=["pool", "volume", "media_id"],
        )
        free = GaugeMetricFamily(
            "nbu_disk_volume_free_bytes",
            "Volume free space in bytes.",
            labels=["pool", "volume", "media_id"],
        )
        used_pct = GaugeMetricFamily(
            "nbu_disk_volume_used_percent",
            "Volume used percent, derived from raw and free bytes.",
            labels=["pool", "volume", "media_id"],
        )
        repl_src = GaugeMetricFamily(
            "nbu_disk_volume_replication_source",
            "1 if the volume is a replication source.",
            labels=["pool", "volume"],
        )
        repl_tgt = GaugeMetricFamily(
            "nbu_disk_volume_replication_target",
            "1 if the volume is a replication target.",
            labels=["pool", "volume"],
        )

        for entry in pools:
            attrs = _job_attrs(entry)
            pool_name = _as_str(attrs.get("name") or attrs.get("diskPoolName"))
            if not pool_name:
                continue
            for vol in attrs.get("diskVolumes") or []:
                if not isinstance(vol, dict):
                    continue
                vol_name = _as_str(vol.get("name"))
                if not vol_name:
                    continue
                media_id = _as_str(vol.get("diskMediaId"))
                state_str = _as_str(vol.get("state"))
                is_up = 1.0 if state_str.upper() == "UP" else 0.0
                raw_b = _as_float(vol.get("rawSizeBytes"))
                free_b = _as_float(vol.get("freeSizeBytes"))

                up.add_metric([pool_name, vol_name, media_id], is_up)
                if raw_b > 0:
                    cap.add_metric([pool_name, vol_name, media_id], raw_b)
                    free.add_metric([pool_name, vol_name, media_id], free_b)
                    used_pct.add_metric(
                        [pool_name, vol_name, media_id],
                        100.0 * (1.0 - free_b / raw_b),
                    )
                repl_src.add_metric(
                    [pool_name, vol_name], 1.0 if vol.get("isReplicationSource") else 0.0
                )
                repl_tgt.add_metric(
                    [pool_name, vol_name], 1.0 if vol.get("isReplicationTarget") else 0.0
                )

        yield up
        yield cap
        yield free
        yield used_pct
        yield repl_src
        yield repl_tgt


# --------------------------------------------------------------------------- #
# Catalog backup collector — derived from the shared jobs cache
# --------------------------------------------------------------------------- #


class CatalogCollector:
    name = "catalog"

    def __init__(
        self,
        cfg: Config,
        jobs_cache: TTLCache[list[dict[str, Any]]],
    ) -> None:
        self._cfg = cfg.collectors.catalog
        self._types = {t.lower() for t in cfg.collectors.catalog.policyTypeValues}
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

    def __init__(self, client: NBUClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg.collectors
        self._lock = threading.Lock()
        self._error_totals: dict[str, int] = {}

        collectors = cfg.collectors
        # Jobs first: clients, policies, storage units, and catalog all read
        # from its TTL cache.
        self._jobs_sub = JobsCollector(client, cfg) if collectors.jobs.enabled else None
        jobs_cache = self._jobs_sub.cache if self._jobs_sub is not None else None

        self._clients_sub: ClientsCollector | None = None
        if collectors.clients.enabled:
            if jobs_cache is None:
                LOGGER.warning(
                    "clients collector requires jobs collector to be enabled "
                    "(NBU 10.0 has no /config/hosts endpoint); skipping clients"
                )
            else:
                self._clients_sub = ClientsCollector(client, cfg, jobs_cache)

        self._policies_sub: PoliciesCollector | None = None
        if collectors.policies.enabled:
            if jobs_cache is None:
                LOGGER.warning(
                    "policies collector requires jobs collector to be enabled; "
                    "skipping policies"
                )
            else:
                self._policies_sub = PoliciesCollector(client, cfg, jobs_cache)

        self._storage_units_sub: StorageUnitsCollector | None = None
        if collectors.storageUnits.enabled:
            if jobs_cache is None:
                LOGGER.warning(
                    "storage_units collector relies on the jobs cache for label "
                    "back-fill; skipping storage_units"
                )
            else:
                self._storage_units_sub = StorageUnitsCollector(client, cfg, jobs_cache)

        self._disk_pools_sub = (
            DiskPoolsCollector(client, cfg) if collectors.diskPools.enabled else None
        )
        self._storage_servers_sub = (
            StorageServersCollector(client, cfg) if collectors.storageServers.enabled else None
        )
        self._msdp_sub: MSDPCollector | None = None
        if collectors.msdp.enabled and self._disk_pools_sub is not None:
            self._msdp_sub = MSDPCollector(client, cfg, self._disk_pools_sub.cache)
        self._disk_volumes_sub: DiskVolumesCollector | None = None
        if collectors.diskVolumes.enabled and self._disk_pools_sub is not None:
            self._disk_volumes_sub = DiskVolumesCollector(cfg, self._disk_pools_sub.cache)
        self._catalog_sub: CatalogCollector | None = None
        if collectors.catalog.enabled and self._jobs_sub is not None:
            self._catalog_sub = CatalogCollector(cfg, self._jobs_sub.cache)

    def _enabled_map(self) -> dict[str, bool]:
        return {
            "jobs": self._cfg.jobs.enabled,
            "clients": self._cfg.clients.enabled,
            "policies": self._cfg.policies.enabled,
            "storage_units": self._cfg.storageUnits.enabled,
            "disk_pools": self._cfg.diskPools.enabled,
            "disk_volumes": self._cfg.diskVolumes.enabled,
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
            self._disk_volumes_sub,
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
