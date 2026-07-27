PYTHON := .venv/bin/python
SETUP_STAMP := .venv/.graveyard-shift-ready

.PHONY: help setup check test verify simulate tick demo-ready demo-stop

help:
	@echo "make setup       - create .venv and install local dependencies"
	@echo "make check       - everything below that needs no credentials"
	@echo "make test        - unit and integration tests"
	@echo "make verify      - admission/watch convergence checks"
	@echo "make simulate    - replay the whole workflow against fakes"
	@echo "make tick        - trigger one reconciliation pass (authenticated)"
	@echo "make demo-ready  - get this machine ready to record: converged, armed, tabs live"
	@echo "make demo-stop   - stop the orchestrator and discard the demo run"

# One target to run before committing. All three layers catch different things:
# the tests cover units, verify covers admission convergence, and simulate
# replays the whole lifecycle against deliberately unkind fakes.
check: test verify simulate

setup: $(SETUP_STAMP)

$(SETUP_STAMP): requirements.txt requirements-dev.txt
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt
	@touch $(SETUP_STAMP)

test:
	$(PYTHON) -m pytest tests/ -q

verify:
	$(PYTHON) scripts/verify_convergence.py

simulate:
	$(PYTHON) scripts/simulate.py

tick:
	@$(PYTHON) scripts/tick.py

demo-ready:
	@$(PYTHON) scripts/demo_preflight.py

demo-stop:
	@$(PYTHON) scripts/demo_preflight.py --stop
