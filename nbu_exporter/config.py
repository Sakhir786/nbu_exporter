"""Configuration dataclasses, YAML loader and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
    paginationStyle: str = "auto"


@dataclass
class HealthCollectorConfig:
    enabled: bool = True


@dataclass
class JobsCollectorConfig:
    enabled: bool = True
    lookbackHours: int = 24
    pageSize: int = 500
    maxPages: int = 200
    cacheTTLSeconds: int = 60


@dataclass
class JobStatesCollectorConfig:
    enabled: bool = True


@dataclass
class ClientsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300


@dataclass
class PoliciesCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 600


@dataclass
class StorageUnitsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    cloudTypePattern: str = "^(amazon|wasabi|azure|gcp|google).*"


@dataclass
class DiskPoolsCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300
    upStateValues: list[int] = field(default_factory=lambda: [2, 1])


@dataclass
class StorageServersCollectorConfig:
    enabled: bool = True
    cacheTTLSeconds: int = 300


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
        if self.nbu.paginationStyle not in ("auto", "jsonapi", "legacy"):
            raise ValueError(
                "nbu.paginationStyle must be one of: auto, jsonapi, legacy"
            )
        if self.server.shutdownTimeoutSeconds <= 0:
            raise ValueError("server.shutdownTimeoutSeconds must be > 0")
        if not self.collectors.any_enabled():
            raise ValueError("at least one collector must be enabled")

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
        if self.collectors.jobs.pageSize <= 0:
            raise ValueError("collectors.jobs.pageSize must be > 0")
        if self.collectors.jobs.maxPages <= 0:
            raise ValueError("collectors.jobs.maxPages must be > 0")


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


def load_config(path: str | Path) -> Config:
    """Load a YAML config file and return a validated Config."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping in {path}")
    cfg = Config()
    _merge_dataclass(cfg, raw)
    cfg.validate()
    return cfg
