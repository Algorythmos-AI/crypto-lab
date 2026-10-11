# Crypto Lab tasks. `make` (or `make help`) lists them.
# The live checkout on the host changes only through `make deploy` (the deploy gate, wt.ops.deploy).
#
# Origin: trading-lab-research `Makefile` at c8d92e1 (trimmed). Removed: every stocks, dashboard, Mac-agent and
# host-seeding target. Changed: `kill`, `unkill` and the status targets act on this repository's one desk;
# `schema` became `contracts`. Added: `init`, `desk-check`, `contracts`, `overlay-check`, `parity`, `test-ml`,
# `sync-venv`.

SHELL := /bin/bash
PY ?= .venv/bin/python
PY_ML ?= .venv-ml/bin/python
export PYTHONPATH := src
export PYTHONDONTWRITEBYTECODE := 1

# Modules held to `mypy --strict`. New modules join this list in the pull request that adds them.
TYPED_MODULES := src/wt/ops/alerts.py src/wt/ops/schedule.py src/wt/ops/locks.py \
                 src/wt/ops/heartbeat.py src/wt/ops/preflight.py src/wt/ops/jobs.py \
                 src/wt/ops/deploy.py src/wt/ops/ci.py src/wt/ops/publish.py \
                 src/wt/research/trials.py src/wt/ops/thresholds.py src/wt/ops/host.py \
                 src/wt/ops/units.py src/wt/ops/hc.py src/wt/core/ledger.py src/wt/ops/r2.py \
                 src/wt/ops/backup.py src/wt/ops/init.py src/wt/ops/contract.py \
                 src/wt/core/desk.py src/wt/crypto/data.py src/wt/crypto/indicators.py src/wt/crypto/quality.py \
                 src/wt/crypto/strategy.py src/wt/crypto/book.py src/wt/crypto/risk.py src/wt/crypto/features.py \
                 src/wt/crypto/labels.py src/wt/crypto/cycle.py src/wt/crypto/snapshot.py src/wt/crypto/control.py \
                 src/wt/crypto/rules.py src/wt/crypto/sleeves.py src/wt/crypto/backtest.py \
                 src/wt/crypto/scorer.py src/wt/ml/modelfile.py src/wt/crypto/signals.py src/wt/crypto/challengers.py \
                 src/wt/crypto/outcomes.py src/wt/crypto/promotion.py src/wt/crypto/learn.py \
                 src/wt/crypto/harvest.py

.DEFAULT_GOAL := help
.PHONY: help bootstrap bootstrap-ml lock-ml sync-venv sync-ml lint typecheck test test-ml ci hooks status gate deploy \
        rollback preflight publish publish-verify contracts overlay-check parity init desk-check kill unkill \
        reset-crypto-latch crypto-learning-off crypto-learning-on crypto-harvest-off crypto-harvest-on \
        crypto-harvest crypto-challengers

help: ## List the tasks
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

bootstrap: ## Create .venv from the lockfile (Python 3.12)
	uv venv --python 3.12 .venv
	uv pip install --require-hashes --python $(PY) -r requirements.lock.txt
	uv pip install --require-hashes --python $(PY) -r requirements-dev.lock.txt

bootstrap-ml: ## Create .venv-ml from its own lockfile (the learning job and the scorer; DEC-0016)
	uv venv --python 3.12 .venv-ml
	uv pip install --require-hashes --python $(PY_ML) -r requirements-ml.lock.txt

lock-ml: ## Recompile the ML lockfile for the host (Linux x86-64), with hashes
	uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes \
		requirements-ml.in -o requirements-ml.lock.txt

lint: ## Ruff, bug-class rules (see pyproject.toml)
	$(PY) -m ruff check .

typecheck: ## mypy --strict on the typed modules
	$(PY) -m mypy --strict $(TYPED_MODULES)

test: ## The trading suite (pytest)
	$(PY) -m pytest

test-ml: ## The ML suite, in the ML environment (make bootstrap-ml first)
	WT_ML_REQUIRED=1 $(PY_ML) -m unittest discover -s tests/ml -t .

ci: lint typecheck test ## What the `test` check runs. The `ml` check is `make test-ml`

hooks: ## Install the pre-commit hooks (gitleaks, runtime-path guard, ruff)
	pre-commit install

contracts: ## Regenerate the snapshot contract: contracts/crypto.schema.json, crypto.v1.json and SHA256SUMS
	$(PY) -c "import json; from wt.crypto.snapshot import to_schema, SCHEMA_PATH; SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True); SCHEMA_PATH.write_text(json.dumps(to_schema(), indent=2) + '\\n')"
	$(PY) scripts/gen_crypto_fixture.py
	$(PY) -m wt.ops.contract > contracts/SHA256SUMS
	@echo "contracts/ regenerated. If it changed, the dashboard must vendor it before this is deployed."

overlay-check: ## Every file made by tools/from-monorepo/overlay is still what the overlay holds
	$(PY) tools/from-monorepo/overlay.py check

parity: ## The parity proof against the monorepo: make parity MONOREPO=/path/to/clone
	@test -n "$(MONOREPO)" || (echo "usage: make parity MONOREPO=/path/to/a/clone/of/trading-lab-research" && exit 2)
	tools/parity.sh "$(MONOREPO)"

# ---- operations on the live checkout. Every command that changes it goes through the deploy gate. ----

status: ## What's going on: the desk's state, kill switch, last job runs, firing alerts, preflight
	$(PY) -m wt.ops.jobs status

gate: ## Is it safe to change the live checkout right now? (no daily job running)
	$(PY) -m wt.ops.deploy gate

deploy: ## Gate; main's tip (or SHA=...) if green; fast-forward, sync venv, smoke (rolls back on failure). STAGE=1: full tests first
	$(PY) -m wt.ops.deploy deploy $(if $(SHA),--sha $(SHA),) $(if $(STAGE),--stage,)

sync-venv: ## Make .venv exactly the lockfile and stamp it for preflight (a first install; a deploy does it itself)
	$(PY) -c "from wt.ops.deploy import sync_venv; sync_venv()"

sync-ml: ## Finish or retry the ML environment's sync (a deploy does it; this is for one left pending or failed)
	$(PY) -m wt.ops.deploy sync-ml

rollback: ## Back to a runtime-* tag: make rollback TAG=runtime-YYYYMMDD-N
	@test -n "$(TAG)" || (echo "usage: make rollback TAG=runtime-YYYYMMDD-N" && exit 2)
	$(PY) -m wt.ops.deploy rollback $(TAG)

preflight: ## Run every job's preflight as its unit would
	@for j in crypto dashboard-crypto crypto-challengers crypto-learn; do echo "== $$j"; \
	  $(PY) -m wt.ops.jobs run $$j --preflight-only || status=1; done; exit $${status:-0}

publish: ## Build, validate and publish the desk's snapshot now (DRY_RUN=1 to only build it)
	$(PY) -m wt.crypto.snapshot $(if $(DRY_RUN),--dry-run,)

publish-verify: ## Publish now, then read /api/health back and check it serves this snapshot
	$(PY) -m wt.crypto.snapshot --verify

# ---- the desk ----

desk-check: ## Does the desk have state here? Jobs refuse to run without it (CRY-DEC-0001)
	@$(PY) -m wt.ops.init --check

init: ## OWNER: make a NEW desk (fresh book, no history, kill switch on). Never for a desk that is being moved in
	@$(PY) -m wt.ops.init --new-desk

kill: ## Stop new entries (exits keep being managed): make kill REASON="..."
	@$(PY) -m wt.ops.init --check >/dev/null || { $(PY) -m wt.ops.init --check; exit 2; }
	@f="$$($(PY) -c 'from wt.core.desk import DESKS; print(DESKS["crypto"].kill_file)')"; \
	  printf '%s\n' "$${REASON:-paused by make kill} ($$(date '+%Y-%m-%d %H:%M %Z'))" > "$$f" && echo "crypto KILL switch ON" && cat "$$f"

unkill: ## Allow entries again (refused while a bar cycle is running). `DESK=crypto` is accepted and ignored
	@$(PY) -m wt.crypto.control unkill

reset-crypto-latch: ## OWNER: clear the desk's daily-loss latch
	@$(PY) -m wt.crypto.control reset-latch

crypto-learning-off: ## OWNER: stop the model acting and the challengers opening trades (exits are still managed)
	@$(PY) -m wt.crypto.control learning-off

crypto-learning-on: ## OWNER: allow the model and the challengers again
	@$(PY) -m wt.crypto.control learning-on

crypto-harvest-off: ## OWNER: stop the data-harvest books opening trades (DEC-0027; exits are still managed)
	@$(PY) -m wt.crypto.control harvest-off

crypto-harvest-on: ## OWNER: allow the data-harvest books again
	@$(PY) -m wt.crypto.control harvest-on

crypto-harvest: ## The data-harvest books: signals, entries, exits, open positions
	@$(PY) -m wt.crypto.harvest --status

crypto-challengers: ## The challengers' record: who was drawn, the backtest verdicts, who is live
	@$(PY) -m wt.crypto.challengers --status
