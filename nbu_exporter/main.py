"""CLI entry point, signal handling and HTTP server bootstrap."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import REGISTRY  # noqa: F401  (re-exported for tests)

from nbu_exporter import __version__
from nbu_exporter.client import NBUClient
from nbu_exporter.collectors import NBUCollector
from nbu_exporter.config import Config, load_config

LOGGER = logging.getLogger("nbu_exporter")


def _configure_logging(level: str, fmt: str) -> None:
    """Configure the root logger from a level string and format choice."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    if fmt == "json":
        formatter: logging.Formatter = _JSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)


class _JSONFormatter(logging.Formatter):
    """Simple JSON log line formatter using only stdlib."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _parse_listen_address(addr: str) -> tuple[str, int]:
    """Split a ``host:port`` string into a tuple suitable for ``HTTPServer``."""
    if ":" not in addr:
        raise ValueError(f"listen address must include port (got {addr!r})")
    host, port_s = addr.rsplit(":", 1)
    return host or "0.0.0.0", int(port_s)


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler that serves /metrics and /health."""

    server_version = f"nbu-exporter/{__version__}"

    # Annotate as ClassVar-ish — set by the server bootstrap below.
    registry: CollectorRegistry
    client: NBUClient

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.debug("http %s - " + format, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self.path.startswith("/metrics"):
            output = generate_latest(self.registry)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
            return
        if self.path.startswith("/health"):
            ok = self.client.health()
            body = json.dumps({"status": "ok", "api_reachable": ok}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", ""):
            body = (
                b"<html><head><title>NBU Exporter</title></head>"
                b"<body><h1>NBU Exporter</h1>"
                b"<p><a href='/metrics'>/metrics</a> "
                b"<a href='/health'>/health</a></p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "not found")


def _build_handler(registry: CollectorRegistry, client: NBUClient) -> type[_Handler]:
    """Return a handler class bound to a specific registry/client pair."""

    class BoundHandler(_Handler):
        pass

    BoundHandler.registry = registry
    BoundHandler.client = client
    return BoundHandler


def _run_server(
    cfg: Config,
    registry: CollectorRegistry,
    client: NBUClient,
    collector: NBUCollector,
) -> int:
    """Block serving HTTP until SIGINT/SIGTERM is received."""
    host, port = _parse_listen_address(cfg.server.listenAddress)
    handler = _build_handler(registry, client)
    httpd = ThreadingHTTPServer((host, port), handler)

    state: dict[str, Any] = {
        "cfg": cfg,
        "client": client,
        "registry": registry,
        "collector": collector,
    }
    stop_event = threading.Event()

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("received signal %d, shutting down", signum)
        stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    def reload_config(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("received SIGHUP, reloading config")
        try:
            new_cfg = load_config(state["config_path"])
        except (OSError, ValueError) as exc:
            LOGGER.error("config reload failed, keeping current config: %s", exc)
            return
        new_client = NBUClient(new_cfg.nbu)
        new_collector = NBUCollector(new_client, new_cfg)
        old_client = state["client"]
        old_collector = state["collector"]
        reg = state["registry"]
        reg.unregister(old_collector)
        reg.register(new_collector)
        state["cfg"] = new_cfg
        state["client"] = new_client
        state["collector"] = new_collector
        _Handler.client = new_client
        _configure_logging(new_cfg.logging.level, new_cfg.logging.format)
        old_client.close()
        LOGGER.info("config reload complete")

    state["config_path"] = getattr(cfg, "_source_path", None)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, reload_config)

    LOGGER.info(
        "nbu-exporter %s listening on %s — nbu=%s",
        __version__,
        cfg.server.listenAddress,
        client.describe(),
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        client.close()
    LOGGER.info("nbu-exporter stopped")
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    """Process command-line arguments and launch the exporter."""
    parser = argparse.ArgumentParser(prog="nbu-exporter")
    parser.add_argument(
        "--config",
        default="/etc/nbu-exporter/config.yaml",
        help="path to the YAML config file",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print version and exit",
    )
    parser.add_argument(
        "--web-listen-address",
        default=None,
        help="override server.listenAddress from config",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"nbu-exporter {__version__}")
        return 0

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"failed to load config from {args.config}: {exc}\n")
        return 1
    cfg._source_path = args.config  # type: ignore[attr-defined]

    if args.web_listen_address:
        cfg.server.listenAddress = args.web_listen_address

    _configure_logging(cfg.logging.level, cfg.logging.format)

    client = NBUClient(cfg.nbu)
    collector = NBUCollector(client, cfg)
    REGISTRY.register(collector)

    return _run_server(cfg, REGISTRY, client, collector)


if __name__ == "__main__":
    sys.exit(cli_main())
