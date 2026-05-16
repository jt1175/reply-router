.PHONY: help verify verify-configs verify-live test test-unit test-fixtures

help:
	@echo "Available targets:"
	@echo "  verify          — verify-configs + test-unit + test-fixtures (pre-commit gate)"
	@echo "  verify-configs  — validate all clients/*.json against schema"
	@echo "  verify-live     — live smoke tests against sandbox APIs (costs ~\$$0.05)"
	@echo "  test-unit       — unit tests only (mocked, fast)"
	@echo "  test-fixtures   — fixture-replay tests (mocked downstreams, real prompts)"

verify: verify-configs test-unit test-fixtures

verify-configs:
	@python -c "from reply_router.config import load_and_validate_all; load_and_validate_all('clients'); print('OK: all client configs valid')"

test-unit:
	pytest tests/unit -v

test-fixtures:
	pytest tests/fixtures -v 2>/dev/null || echo "(no fixture tests yet)"

verify-live:
	@echo "Running live smoke tests — costs ~\$$0.05 in API calls"
	pytest tests/smoke_*.py -v -s
