"""Module entry point: ``python -m nbu_exporter``."""

from __future__ import annotations

import sys

from nbu_exporter.main import cli_main

if __name__ == "__main__":
    sys.exit(cli_main())
