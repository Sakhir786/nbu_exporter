# nbu-exporter

A standalone Prometheus exporter for Veritas NetBackup, written in Python.

The goal is comprehensive per-asset NetBackup observability with the smallest
possible dependency footprint and a clean, config-driven architecture that
anyone running NetBackup against any environment can use without code changes.

## Architecture

- **Minimal dependencies.** Only `prometheus_client`, `pyyaml`, and `requests`.
  Everything else is the Python standard library.
- **Single package, flat layout.** Six modules in `nbu_exporter/`, under
  ~1,500 lines including docstrings.
- **Config-driven.** Every environment-specific value lives in `config.yaml`.
  Zero hostnames, paths, type strings, or numeric codes hardcoded in Python.
- **Single service, single port.** Systemd-managed, hardened unit running as
  the unprivileged `nbu-exporter` user.
- **Python 3.10+.** Uses modern type hints and `match`-friendly idioms.

## Quick start

```bash
# Development install
make install-dev
.venv/bin/nbu-exporter --version

# System install (build wheel, install under /opt, systemd unit)
make install-system
sudo cp config.yaml.example /etc/nbu-exporter/config.yaml
sudoedit /etc/nbu-exporter/config.yaml         # set host + apiKey
sudo systemctl enable --now nbu-exporter

# One-shot remote build + install
REPO_URL=https://github.com/Sakhir786/nbu_exporter.git ./scripts/deploy.sh
```

## Configuration

All settings live in `/etc/nbu-exporter/config.yaml`. See
`config.yaml.example` for the canonical, fully-commented reference.

| Key | Default | Description |
|---|---|---|
| `server.listenAddress` | `0.0.0.0:2112` | `host:port` to bind |
| `server.shutdownTimeoutSeconds` | `10` | Grace period on SIGTERM |
| `logging.level` | `info` | `debug` / `info` / `warning` / `error` |
| `logging.format` | `text` | `text` or `json` |
| `nbu.scheme` / `host` / `port` / `basePath` | `https` / – / `1556` / `/netbackup` | NBU master URL pieces |
| `nbu.apiVersion` | `3.0` | NetBackup REST API version |
| `nbu.contentType` | `application/vnd.netbackup+json;version=3.0` | Accept/Content-Type header |
| `nbu.apiKey` | – | NBU REST API key (required) |
| `nbu.insecureSkipVerify` | `false` | Skip TLS verification |
| `nbu.caCertFile` | – | Optional custom CA bundle path |
| `nbu.requestTimeoutSeconds` | `30` | Per-request HTTP timeout |
| `nbu.maxRetries` | `3` | Retry budget for 5xx / 429 / connection errors |
| `nbu.retryBackoffSeconds` | `2` | Exponential backoff factor |
| `nbu.pagination.pageSize` | `100` | Records per page (default-safe for NBU 10.0; bump to 500 on 10.1+) |
| `nbu.pagination.maxPages` | `200` | Safety cap on the pagination walk |
| `nbu.pagination.style` | `jsonapi` | `jsonapi` (`page[limit]`/`page[offset]`) or `legacy` (`limit`/`offset`) |
| `collectors.<name>.pageSize` | inherits | Per-collector override for pagination page size |
| `collectors.<name>.maxPages` | inherits | Per-collector override for pagination cap |
| `collectors.*.enabled` | `true` | Per-collector on/off |
| `collectors.jobs.lookbackHours` | `24` | Job history window |
| `collectors.jobs.pageSize` | `500` | JSON:API page size |
| `collectors.jobs.maxPages` | `200` | Pagination safety cap |
| `collectors.*.cacheTTLSeconds` | varies | Per-collector cache lifetime |
| `collectors.storageUnits.cloudTypePattern` | `^(amazon\|wasabi\|azure\|gcp\|google).*` | STU types treated as "cloud" |
| `collectors.diskPools.upStateValues` | `[2, 1]` | Numeric states considered UP |
| `collectors.msdp.serverTypePattern` | `(?i)(msdp\|puredisk)` | Server types eligible for MSDP metrics |
| `collectors.catalog.policyTypeValues` | `["NBU-Catalog"]` | Policy types representing catalog backups |

## Metrics

### Core metrics

| Metric | Type | Labels |
|---|---|---|
| `nbu_up` | gauge | – |
| `nbu_api_version` | gauge | `version` |
| `nbu_last_scrape_timestamp_seconds` | gauge | `source` |
| `nbu_disk_bytes` | gauge | `name`, `type`, `size` |
| `nbu_jobs_count` | gauge | `action`, `policy_type`, `status` |
| `nbu_jobs_bytes` | gauge | `action`, `policy_type`, `status` |
| `nbu_status_count` | gauge | `action`, `status` |

### Additional metrics

#### Exporter operational
- `nbu_exporter_scrape_duration_seconds{collector=...}` (gauge)
- `nbu_exporter_scrape_errors_total{collector=...}` (counter)
- `nbu_exporter_collector_enabled{collector=...}` (gauge)
- `nbu_exporter_build_info{version=...,python_version=...}` (gauge)

#### Job lifecycle
- `nbu_jobs_by_state{state,action,policy_type}` (gauge)

#### Per-client
- `nbu_client_last_successful_backup_timestamp{client,policy,schedule}` (gauge)
- `nbu_client_last_attempt_status{client,policy,schedule}` (gauge)
- `nbu_client_info{client,hardware,os}` (gauge)

#### Per-policy
- `nbu_policy_jobs_count{policy_name,policy_type,action,status}` (gauge)
- `nbu_policy_jobs_bytes{policy_name,policy_type,action,status}` (gauge)
- `nbu_policy_info{policy_name,policy_type,active}` (gauge)

#### Per-disk-pool
- `nbu_disk_pool_capacity_bytes{pool,storage_server,server_type}` (gauge)
- `nbu_disk_pool_used_bytes{pool,storage_server,server_type}` (gauge)
- `nbu_disk_pool_used_ratio{pool,storage_server,server_type}` (gauge)
- `nbu_disk_pool_state{pool,storage_server,server_type}` (gauge)
- `nbu_disk_pool_up{pool}` (gauge)
- `nbu_disk_pool_volumes{pool}` (gauge)

#### Per-storage-server
- `nbu_storage_server_info{name,type,category}` (gauge)
- `nbu_storage_server_state{name,type}` (gauge)

#### MSDP
- `nbu_msdp_dedup_ratio{pool,storage_server}` (gauge)
- `nbu_msdp_logical_bytes{pool,storage_server}` (gauge)
- `nbu_msdp_physical_bytes{pool,storage_server}` (gauge)

#### Catalog backup
- `nbu_catalog_backup_last_timestamp` (gauge)
- `nbu_catalog_backup_last_status` (gauge)
- `nbu_catalog_backup_size_bytes` (gauge)

## Systemd

The shipped unit (`systemd/nbu-exporter.service`) runs the exporter as the
unprivileged `nbu-exporter` user with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, and other hardening directives.

```bash
sudo useradd -r -s /sbin/nologin nbu-exporter
sudo mkdir -p /var/log/nbu-exporter
sudo chown nbu-exporter:nbu-exporter /var/log/nbu-exporter
sudo chown nbu-exporter:nbu-exporter /etc/nbu-exporter/config.yaml
sudo chmod 0640 /etc/nbu-exporter/config.yaml
sudo systemctl enable --now nbu-exporter
```

`SIGHUP` reloads the config file in place; if the reloaded config fails
validation, the exporter keeps running with the previous good config and
logs the error.

## Validation

```bash
sudo systemctl status nbu-exporter --no-pager
curl -s localhost:2112/health
curl -s localhost:2112/metrics | grep -E '^# HELP nbu_' | sort
curl -s localhost:2112/metrics | grep -E '^nbu_jobs_count{' | awk '{sum+=$NF} END {print "Total jobs:", sum}'
curl -s localhost:2112/metrics | grep -c '^nbu_client_'
curl -s localhost:2112/metrics | grep -c '^nbu_disk_pool_'
curl -s localhost:2112/metrics | grep -c '^nbu_policy_'
curl -s localhost:2112/metrics | grep -c '^nbu_msdp_'
curl -s localhost:2112/metrics | grep nbu_catalog
curl -s localhost:2112/metrics | grep nbu_exporter_scrape_duration
curl -s localhost:2112/metrics | grep nbu_exporter_scrape_errors_total
sudo journalctl -u nbu-exporter -n 100 --no-pager
```

## Upgrading

The exporter takes care never to clobber the operator's config when a
new release introduces schema additions.

- `/etc/nbu-exporter/config.yaml` is **never overwritten** by
  `scripts/deploy.sh`. Your edits survive every upgrade.
- When the new release ships new config keys, the deploy script writes
  the latest example to `/etc/nbu-exporter/config.yaml.new` and prints
  a banner listing which keys are new. Defaults apply for those keys
  until you merge them.
- At service start the exporter logs an INFO line for every optional
  block missing from your config, e.g.

  ```
  config: nbu.pagination not present in config, using defaults
    (pageSize=100, maxPages=200, style=jsonapi)
  ```

  Watch with `sudo journalctl -u nbu-exporter -n 50 --no-pager`.

### Compare and merge new keys

```bash
# Quick diff between your live config and the latest example
make config-diff

# Or, after a deploy run that produced config.yaml.new:
sudo diff -u /etc/nbu-exporter/config.yaml /etc/nbu-exporter/config.yaml.new
```

Defaults are chosen to be safe across the supported NetBackup versions —
you only need to add explicit values when you change NBU version (see
[Compatibility](#compatibility)) or want to tune a knob away from its
default.

## Development

```bash
make install-dev
make lint           # ruff
make type           # mypy --strict nbu_exporter
make test           # pytest with coverage
make check          # all of the above
```

## Compatibility

Every pagination parameter the exporter sends is config-driven. To support
a new NetBackup version you change `config.yaml`, never the code.

### NetBackup version → pagination matrix

| NBU version | `apiVersion` | `pageSize` max | `style`   |
|-------------|--------------|----------------|-----------|
| 8.x         | `3.0`        | 100            | `legacy`  |
| 9.x         | `5.0`        | 100            | `legacy`  |
| 10.0        | `7.0`        | 100            | `jsonapi` |
| 10.1 – 10.4 | `11.0`       | 500            | `jsonapi` |
| 10.5        | `12.0`       | 500+           | `jsonapi` |
| 11.x+       | `13.0`       | 500+           | `jsonapi` |

Set those three values under `nbu:` in `/etc/nbu-exporter/config.yaml`:

```yaml
nbu:
  apiVersion: "7.0"
  contentType: "application/vnd.netbackup+json;version=7.0"
  pagination:
    pageSize: 100
    maxPages: 200
    style: jsonapi
```

Per-collector overrides (`collectors.<name>.pageSize`,
`collectors.<name>.maxPages`) are honoured; values left unset inherit
`nbu.pagination`.

## License

MIT — see [LICENSE](LICENSE).
