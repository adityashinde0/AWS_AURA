# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands

```bash
# All tests (from project root)
python3 -m pytest

# Single test file
python3 -m pytest tests/unit/test_websocket_handler.py

# Single test by name
python3 -m pytest tests/unit/test_websocket_handler.py::test_handle_relay_success_returns_ack

# Contract tests only
python3 -m pytest tests/contract/

# Skip known boto3-requiring failures (no AWS SDK in local env)
python3 -m pytest --ignore=tests/e2e/test_pre_meeting_e2e.py \
  --ignore=tests/unit/test_bedrock_adapter.py \
  --ignore=tests/unit/test_calendar_adapter.py
```

`pytest.ini` sets `pythonpath = .` so all imports resolve from the project root — no `src/` prefix needed.

## Known broken tests (pre-existing, not your fault)

`test_pre_meeting_e2e.py`, `test_bedrock_adapter.py`, and `test_calendar_adapter.py` fail at **collection** with `ModuleNotFoundError: No module named 'boto3'` in environments lacking the AWS SDK. This is an environment constraint, not a code bug. The other 508 tests pass without boto3 (and all 517 tests pass when boto3 and botocore are installed).

## Lambda module imports

Lambda handlers live in `lambda/` but are imported via `importlib.import_module("lambda.focus_guardian")` — not `from lambda import ...` — because `lambda` is a Python keyword. Every test file in the repo uses this pattern.

## Separate requirements files

There is **no top-level `requirements.txt`** and no `pyproject.toml`. Dependencies are split:
- `lambda/requirements.txt` — `boto3>=1.35.0`, `pydantic>=2.9.0`
- `standalone_agent/requirements.txt` — `requests>=2.32.0`
- `mcp_server/requirements.txt` — `mcp>=1.4.0`

Install the lambda deps to run the full test suite: `pip install pydantic boto3`.

## Structured logging pattern

All log output uses the `_log(level, event, **fields)` helper defined in each Lambda module. It emits a single JSON line to `stdout` with `timestamp`, `level`, `service`, `event`, and caller-supplied keyword fields. **Never use bare `print()` for operational output.** No raw user content, evidence text, auth tokens, or prompt bodies may appear in any log field.

## Testing log output

The `capture_logs(monkeypatch, module)` helper in `tests/contract/test_modernization_contract.py` patches `builtins.print` (not just module-level `print`) because both Lambda modules call `print` at call-time from builtins. Module-level patching alone misses emissions.

## focus_guardian: `FocusProposal` alias field

`FocusProposal.questions` is declared with `alias="3_questions"`. Model output and test fixtures must use the key `"3_questions"` (string, not identifier). Use `model_dump(by_alias=True)` when serialising and `model_validate` / `model_json_schema(by_alias=True)` when building prompts.

## focus_guardian: evidence hallucination guard

After a successful Pydantic parse, `lambda_handler` checks that every ID in `proposal.evidence_ids` is a strict subset of the IDs present in the incoming `request.evidence` list. Any fabricated ID raises `ValueError` → `safe_fallback(request, fallback_reason="hallucinated_evidence")`. Do not remove this check.

## control_plane: `InMemoryDataStore` for tests

`lambda/control_plane.py` exports `InMemoryDataStore` — an in-memory DynamoDB substitute used by all unit and e2e tests that exercise the control plane. Pass it as `store=` to all `handle_*` functions. Do not use `boto3` in tests.

## Policy engine is the only execution gate

`standalone_agent/policy_engine.py` is the **only** component allowed to execute actions. Lambda handlers and model proposals always set `policy_required: True` and return a recommendation; they never execute directly. Tests verify this by asserting `body["policy_required"] is True`.

## JSON schemas

`schemas/websocket/`, `schemas/dynamodb/`, `schemas/eventbridge/`, and `schemas/pre_meeting/` contain JSON Schema files validated in `tests/contract/`. Resolve schema paths with `Path(__file__).resolve().parent.parent.parent / "schemas" / <subdir>` (3 levels up from `tests/contract/`).

## Code style

- `from __future__ import annotations` at the top of every module
- Type hints everywhere; `typing.Any` for external/untyped data
- `Literal[...]` for all constrained string fields (action types, signal types, scopes)
- `@dataclass` for plain data containers; Pydantic `BaseModel` for validated input/output
- `| None` union syntax (not `Optional[...]`)
- Max line length not enforced by a linter config but existing code stays under ~100 chars
