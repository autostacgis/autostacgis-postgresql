PYTHON ?= python3
.PHONY: install dev test build dev-build release release-manifest
install: build
	$(PYTHON) -m pip install --find-links wheels -e '.[dev]'
dev:
	$(PYTHON) -m uvicorn autostacgis_postgresql.app:create_app --factory --reload --host 127.0.0.1 --port 8080
test:
	$(PYTHON) -m pytest tests -q
build:
	$(PYTHON) scripts/build_engine_wheels.py
	$(PYTHON) -m build --wheel --outdir wheels .
release-manifest: build
	$(PYTHON) scripts/release_manifest.py $(VERSION)

dev-build: build
release: release-manifest
