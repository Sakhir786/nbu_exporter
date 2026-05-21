PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: venv install install-dev lint format type test clean check build install-system config-diff

venv:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: venv
	$(BIN)/pip install -e .

install-dev: venv
	$(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check nbu_exporter tests

format:
	$(BIN)/ruff format nbu_exporter tests

type:
	$(BIN)/mypy --strict nbu_exporter

test:
	$(BIN)/pytest -v --cov=nbu_exporter --cov-report=term-missing

check: lint type test

build:
	$(BIN)/pip wheel --no-deps -w dist .

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Show what's new in config.yaml.example compared to the deployed config.
# Exit status is intentionally 0 even if files differ.
config-diff:
	@diff -u /etc/nbu-exporter/config.yaml config.yaml.example || true

install-system: build
	sudo install -d /opt/nbu-exporter
	sudo $(PYTHON) -m venv /opt/nbu-exporter/venv
	sudo /opt/nbu-exporter/venv/bin/pip install --upgrade pip
	sudo /opt/nbu-exporter/venv/bin/pip install dist/*.whl
	sudo ln -sf /opt/nbu-exporter/venv/bin/nbu-exporter /usr/local/bin/nbu-exporter
	sudo mkdir -p /etc/nbu-exporter
	sudo cp -n config.yaml.example /etc/nbu-exporter/config.yaml || true
	sudo install -m 0644 systemd/nbu-exporter.service /etc/systemd/system/nbu-exporter.service
	sudo systemctl daemon-reload
	@echo "Edit /etc/nbu-exporter/config.yaml then: sudo systemctl enable --now nbu-exporter"
