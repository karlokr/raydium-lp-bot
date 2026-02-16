.PHONY: start test test-unit test-integration install

# ── Run the bot ──────────────────────────────────────────────────────
start:
	.venv/bin/python run.py

# ── Run all tests (unit + integration) ───────────────────────────────
test:
	.venv/bin/python -m pytest -p no:anchorpy tests/ -v --tb=short

# ── Unit tests only (default, skips network-dependent tests) ─────────
test-unit:
	.venv/bin/python -m pytest -p no:anchorpy tests/ -v --tb=short -m "not integration"

# ── Integration tests only (requires network + .env) ─────────────────
test-integration:
	.venv/bin/python -m pytest -p no:anchorpy tests/ -v --tb=short -m integration

# ── Install dependencies ─────────────────────────────────────────────
install:
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt
	npm install
