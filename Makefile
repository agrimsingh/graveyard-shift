PYTHON := .venv/bin/python

.PHONY: help verify simulate demo-ready demo-stop

help:
	@echo "make verify      - admission/watch convergence checks (no credentials)"
	@echo "make simulate    - replay the whole workflow against fakes (no credentials)"
	@echo "make demo-ready  - get this machine ready to record: converged, armed, tabs live"
	@echo "make demo-stop   - stop the orchestrator and discard the demo run"

verify:
	$(PYTHON) scripts/verify_convergence.py

simulate:
	$(PYTHON) scripts/simulate.py

demo-ready:
	@$(PYTHON) scripts/demo_preflight.py

demo-stop:
	@$(PYTHON) scripts/demo_preflight.py --stop
