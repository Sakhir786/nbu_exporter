"""Configuration dataclasses, YAML loader and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PAGINATION_STYLES = ("jsonapi", "legacy")


@dataclass
class ServerConfig:
    """HTTP server settings for the exporter itself."""

    listenAddress: str = "0.0.0.0:2112"
    shutdownTimeoutSeconds: int = 10


@dataclass
class LoggingConfig:
    """Logging behaviour for the exporter process."""

    level: str = "info"
    format: str = "text"


@dataclass
class PaginationConfig:
    """Pagination defaults for every NBU REST API endpoint.

    Per-collector blocks may override ``pageSize`` / ``maxPages``; the values
    here are the fallback. ``style`` is master-wide and picked by NBU version
    — see the Compatibility matrix in the README.
    """

    pageSize: int = 100
    maxPages: int = 200
    style: str = "jsonapi"


@dataclass
class NBUConfig:
    """NetBackup master REST API connection settings."""

    scheme: str = "https"
    host: str = ""
    port: int = 1556
    basePath: str = "/netbackup"
    apiVersion: str = "3.0"
    contentType: str = "application/vnd.netbackup+json;version=3.0"
    apiKey: str = ""
    insecureSkipVerify: bool = False
    caCertFile: str = ""
    requestTimeoutSeconds: int = 30
    maxRetries: int = 3
    retryBackoffSeconds: int = 2
    pagination: PaginationConfig = field(default_factory=PaginationConfig)


@dataclass
class HealthCollectorConfig:
    enabled: bool = True


@dataclass
class JobsCollectorConfig:
    enabled: bool = True
    lookbackHours: int = 24
    cacheTTLSeconds: int = 60
    pageSize: int | None = None  # inherit nbu.pagination.pageSize
    maxPages: int | None = None  # inherit nbu.pagination.maxPages


@dataclass
class JobStatesCollectorConfig:
    enabled: bool = True


@dataclass
class ClientsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    pageSize: int | None = None
    maxPages: int | None = None


@dataclass
class PoliciesCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 600
    pageSize: int | None = None
    maxPages: int | None = None


@dataclass
class StorageUnitsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    cloudTypePattern: str = "^(amazon|wasabi|azure|gcp|google).*"
    pageSize: int | None = None
    maxPages: int | None = None


@dataclass
class DiskPoolsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    upStateValues: list[int] = field(default_factory=lambda: [2, 1])
    pageSize: int | None = None
    maxPages: int | None = None


@dataclass
class StorageServersCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    pageSize: int | None = None
    maxPages: int | None = None


@dataclass
class MSDPCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    serverTypePattern: str = "(?i)(msdp|puredisk)"


@dataclass
class CatalogCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 600
    policyTypeValues: list[str] = field(default_factory=lambda: ["NBU-Catalog"])


@dataclass
class CollectorsConfig:
    """Aggregate of every per-collector configuration block."""

    health: HealthCollectorConfig = field(default_factory=HealthCollectorConfig)
    jobs: JobsCollectorConfig = field(default_factory=JobsCollectorConfig)
    jobStates: JobStatesCollectorConfig = field(default_factory=JobStatesCollectorConfig)
    clients: ClientsCollectorConfig = field(default_factory=ClientsCollectorConfig)
    policies: PoliciesCollectorConfig = field(default_factory=PoliciesCollectorConfig)
    storageUnits: StorageUnitsCollectorConfig = field(default_factory=StorageUnitsCollectorConfig)
    diskPools: DiskPoolsCollectorConfig = field(default_factory=DiskPoolsCollectorConfig)
    storageServers: StorageServersCollectorConfig = field(
        default_factory=StorageServersCollectorConfig
    )
    msdp: MSDPCollectorConfig = field(default_factory=MSDPCollectorConfig)
    catalog: CatalogCollectorConfig = field(default_factory=CatalogCollectorConfig)

    def any_enabled(self) -> bool:
        """Return True if at least one non-health collector is enabled."""
        return any(
            [
                self.jobs.enabled,
                self.jobStates.enabled,
                self.clients.enabled,
                self.policies.enabled,
                self.storageUnits.enabled,
                self.diskPools.enabled,
                self.storageServers.enabled,
                self.msdp.enabled,
                self.catalog.enabled,
            ]
        )


@dataclass
class Config:
    """Top-level exporter configuration."""

    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    nbu: NBUConfig = field(default_factory=NBUConfig)
    collectors: CollectorsConfig = field(default_factory=CollectorsConfig)

    def resolve_pagination(
        self, page_size: int | None, max_pages: int | None
    ) -> tuple[int, int, str]:
        """Resolve a collector's effective (pageSize, maxPages, style).

        ``None`` values fall back to ``nbu.pagination``. Style is master-wide.
        """
        p = self.nbu.pagination
        return (
            page_size if page_size is not None else p.pageSize,
            max_pages if max_pages is not None else p.maxPages,
            p.style,
        )

    def validate(self) -> None:
        """Raise ValueError when required fields are missing or invalid."""
        if not self.nbu.host:
            raise ValueError("nbu.host must be set")
        if not self.nbu.apiKey or self.nbu.apiKey == "REPLACE_WITH_NBU_API_KEY":
            raise ValueError("nbu.apiKey must be set to a real key")
        if not self.nbu.apiVersion:
            raise ValueError("nbu.apiVersion must be set")
        if self.nbu.requestTimeoutSeconds <= 0:
            raise ValueError("nbu.requestTimeoutSeconds must be > 0")
        if self.nbu.maxRetries < 0:
            raise ValueError("nbu.maxRetries must be >= 0")
        if self.nbu.retryBackoffSeconds < 0:
            raise ValueError("nbu.retryBackoffSeconds must be >= 0")
        if self.server.shutdownTimeoutSeconds <= 0:
            raise ValueError("server.shutdownTimeoutSeconds must be > 0")
        if not self.collectors.any_enabled():
            raise ValueError("at least one collector must be enabled")

        p = self.nbu.pagination
        if p.pageSize <= 0:
            raise ValueError("nbu.pagination.pageSize must be > 0")
        if p.maxPages <= 0:
            raise ValueError("nbu.pagination.maxPages must be > 0")
        if p.style not in PAGINATION_STYLES:
            raise ValueError(f"nbu.pagination.style must be one of: {', '.join(PAGINATION_STYLES)}")

        for name, ttl in (
            ("jobs", self.collectors.jobs.cacheTTLSeconds),
            ("clients", self.collectors.clients.cacheTTLSeconds),
            ("policies", self.collectors.policies.cacheTTLSeconds),
            ("storageUnits", self.collectors.storageUnits.cacheTTLSeconds),
            ("diskPools", self.collectors.diskPools.cacheTTLSeconds),
            ("storageServers", self.collectors.storageServers.cacheTTLSeconds),
            ("msdp", self.collectors.msdp.cacheTTLSeconds),
            ("catalog", self.collectors.catalog.cacheTTLSeconds),
        ):
            if ttl <= 0:
                raise ValueError(f"collectors.{name}.cacheTTLSeconds must be > 0")

        if self.collectors.jobs.lookbackHours <= 0:
            raise ValueError("collectors.jobs.lookbackHours must be > 0")

        for name, page_size, max_pages in (
            ("jobs", self.collectors.jobs.pageSize, self.collectors.jobs.maxPages),
            ("clients", self.collectors.clients.pageSize, self.collectors.clients.maxPages),
            ("policies", self.collectors.policies.pageSize, self.collectors.policies.maxPages),
            (
                "storageUnits",
                self.collectors.storageUnits.pageSize,
                self.collectors.storageUnits.maxPages,
            ),
            (
                "diskPools",
                self.collectors.diskPools.pageSize,
                self.collectors.diskPools.maxPages,
            ),
            (
                "storageServers",
                self.collectors.storageServers.pageSize,
                self.collectors.storageServers.maxPages,
            ),
        ):
            if page_size is not None and page_size <= 0:
                raise ValueError(f"collectors.{name}.pageSize must be > 0")
            if max_pages is not None and max_pages <= 0:
                raise ValueError(f"collectors.{name}.maxPages must be > 0")


def _merge_dataclass(target: Any, data: dict[str, Any]) -> None:
    """Recursively overlay a dict onto a dataclass instance in place."""
    for key, value in data.items():
        if not hasattr(target, key):
            continue
        current = getattr(target, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(target, key, value)


def _dotted_present(raw: dict[str, Any], dotted_path: str) -> bool:
    """Return True iff a dotted-path key exists in a parsed YAML dict."""
    cur: Any = raw
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


# Optional config blocks worth announcing at startup when absent. Each entry
# is (dotted-path, summary-callable). When a schema change adds a new block
# users may want to merge into their existing config.yaml, add it here so the
# fallback shows up in journald — that closes the upgrade-UX loop.
_OPTIONAL_BLOCKS: list[tuple[str, Any]] = [
    (
        "nbu.pagination",
        lambda c: (
            f"pageSize={c.nbu.pagination.pageSize}, "
            f"maxPages={c.nbu.pagination.maxPages}, "
            f"style={c.nbu.pagination.style}"
        ),
    ),
]


def _detect_fallback_notes(raw: dict[str, Any], cfg: Config) -> list[str]:
    """Return human-readable notes for each optional block that fell back."""
    notes: list[str] = []
    for path, summary in _OPTIONAL_BLOCKS:
        if not _dotted_present(raw, path):
            notes.append(
                f"config: {path} not present in config, using defaults ({summary(cfg)})"
            )
    return notes


def _read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping in {path}")
    return raw


def load_config(path: str | Path) -> Config:
    """Load a YAML config file and return a validated Config."""
    raw = _read_yaml(path)
    cfg = Config()
    _merge_dataclass(cfg, raw)
    cfg.validate()
    return cfg


def load_config_with_notes(path: str | Path) -> tuple[Config, list[str]]:
    """Load a config file and also return fallback-notes for the operator.

    The returned list is suitable for emitting at INFO level after logging
    has been configured — one entry per optional block absent from the YAML.
    """
    raw = _read_yaml(path)
    cfg = Config()
    _merge_dataclass(cfg, raw)
    cfg.validate()
    return cfg, _detect_fallback_notes(raw, cfg)
