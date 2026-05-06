PY := env/bin/python
PYTEST := env/bin/pytest

.PHONY: help install test e2e record clean

help:
	@echo "make install   install dev deps into env/"
	@echo "make test      run layer-1 + layer-2 tests (fast, offline)"
	@echo "make e2e       run live end-to-end against the throwaway GH repo"
	@echo "make record    re-record gh fixtures against the throwaway GH repo"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PYTEST) -q

e2e:
	tests/e2e.sh

record:
	tests/record_gh.sh
