import importlib.util
import json
import sqlite3
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "hermes_poke_eval.py"
SPEC = importlib.util.spec_from_file_location("hermes_poke_eval", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _fixture() -> dict:
    return {
        "version": 1,
        "cases": [
            {"id": f"case-{index}", "input": f"input {index}"}
            for index in range(MODULE.EXPECTED_CASE_COUNT)
        ],
    }


def test_load_fixture_requires_exact_unique_nonempty_cases(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(_fixture()), encoding="utf-8")
    assert len(MODULE.load_fixture(path)["cases"]) == MODULE.EXPECTED_CASE_COUNT

    duplicate = _fixture()
    duplicate["cases"][-1]["id"] = duplicate["cases"][0]["id"]
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        MODULE.load_fixture(path)


def test_result_validity_rejects_empty_and_truncated():
    valid, reasons = MODULE.result_validity(
        {"final_response": ""},
        [{"finish_reason": "length"}],
    )
    assert valid is False
    assert "empty-final-response" in reasons
    assert "truncated-finish:length" in reasons

    valid, reasons = MODULE.result_validity(
        {"final_response": "Risposta completa."},
        [{"finish_reason": "stop"}],
    )
    assert valid is True
    assert reasons == []


def test_tool_event_capture_uses_tool_name_not_call_id():
    capture = MODULE.EventCapture()
    capture.tool_start("call-1", "session_search", {"query": "x"})
    capture.tool_complete("call-1", "session_search", {}, "{}")
    assert capture.tool_events == [
        {"event": "start", "tool": "session_search"},
        {"event": "complete", "tool": "session_search"},
    ]


def test_read_only_policy_allows_reads_and_blocks_mutations():
    capture = MODULE.EventCapture()
    assert capture.read_only_policy(
        tool_name="cronjob", args={"action": "list"}
    ) is None
    assert capture.read_only_policy(
        tool_name="session_search", args={"query": "x"}
    ) is None
    blocked = capture.read_only_policy(
        tool_name="cronjob", args={"action": "update"}
    )
    assert blocked["action"] == "block"
    assert capture.blocked_tool_events == [
        {
            "tool": "cronjob",
            "action": "update",
            "reason": "eval-read-only-policy",
        }
    ]


def test_recover_tool_events_uses_persisted_names(tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "create table messages (id integer, session_id text, role text, "
        "tool_call_id text, tool_calls text, tool_name text, active integer)"
    )
    connection.executemany(
        "insert into messages values (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "session-1",
                "assistant",
                None,
                json.dumps(
                    [
                        {
                            "id": "call-1",
                            "function": {"name": "session_search"},
                        }
                    ]
                ),
                None,
                1,
            ),
            (2, "session-1", "tool", "call-1", None, "session_search", 1),
        ],
    )
    connection.commit()
    connection.close()
    assert MODULE.recover_tool_events(path, "session-1") == [
        {"event": "start", "tool": "session_search"},
        {"event": "complete", "tool": "session_search"},
    ]


def test_atomic_receipt_is_private_and_resume_requires_valid_content(tmp_path):
    MODULE.prepare_private_dir(tmp_path)
    path = tmp_path / "row.json"
    payload = {
        "schema_version": MODULE.SCHEMA_VERSION,
        "kind": MODULE.KIND,
        "fixture_sha256": "a" * 64,
        "case_id": "case-1",
        "valid": True,
        "output": "ok",
    }
    MODULE.atomic_private_json(path, payload)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert MODULE.checkpoint_is_resumable(
        payload, fixture_sha256="a" * 64, case_id="case-1"
    )

    payload["output"] = ""
    assert not MODULE.checkpoint_is_resumable(
        payload, fixture_sha256="a" * 64, case_id="case-1"
    )


def test_api_adapter_accepts_eval_toolset_platform_and_token_budget():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch("gateway.run._resolve_runtime_agent_kwargs") as runtime, \
         patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("run_agent.AIAgent", return_value=MagicMock()) as agent_cls:
        runtime.return_value = {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
        }
        adapter._create_agent(toolset_platform="telegram", max_tokens=4096)

    kwargs = agent_cls.call_args.kwargs
    assert kwargs["max_tokens"] == 4096
    assert "clarify" in kwargs["enabled_toolsets"]
    assert kwargs["platform"] == "api_server"
