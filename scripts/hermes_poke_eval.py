#!/usr/bin/env python3
"""Checkpointed Poke-quality evaluation through Hermes' real gateway agent."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
KIND = "hermes-poke-gateway-eval"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_CASE_TIMEOUT_SECONDS = 180
EXPECTED_CASE_COUNT = 20
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
READ_ONLY_TOOLS = {
    "dbrain_search",
    "obsidian_read",
    "obsidian_search",
    "read_file",
    "search_files",
    "session_search",
    "skill_view",
    "skills_list",
    "vision_analyze",
    "web_extract",
    "web_search",
}
READ_ONLY_ACTIONS = {
    "cronjob": {"get", "list", "show", "status"},
    "kanban": {"list", "runs", "show", "stats", "tail"},
    "todo": {"get", "list", "show"},
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def safe_case_id(value: str) -> str:
    cleaned = SAFE_ID_RE.sub("-", value).strip("-")
    if not cleaned or cleaned != value:
        raise ValueError(f"case id is not filesystem-safe: {value!r}")
    return cleaned


def load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if payload.get("version") != 1 or not isinstance(cases, list):
        raise ValueError("fixture must have version=1 and a cases list")
    if len(cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"fixture must contain exactly {EXPECTED_CASE_COUNT} cases")
    seen: set[str] = set()
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError("every fixture case must be an object")
        case_id = safe_case_id(str(row.get("id") or ""))
        if case_id in seen:
            raise ValueError(f"duplicate fixture case id: {case_id}")
        seen.add(case_id)
        if not str(row.get("input") or "").strip():
            raise ValueError(f"case {case_id} has empty input")
        for field in ("deterministic", "subjective"):
            value = row.get(field, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(f"case {case_id} has invalid {field} criteria")
    return payload


def prepare_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("output directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("output directory must be owner-only mode 0700")


def atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError(f"output target must not be a symlink: {path}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_base_url_host(value: Any) -> str | None:
    if not value:
        return None
    try:
        from utils import base_url_hostname

        return base_url_hostname(str(value)) or None
    except Exception:
        return None


def _identity_file_receipts(hermes_home: Path) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for name in ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"):
        path = hermes_home / name
        if not path.is_file():
            receipts[name] = {"present": False}
            continue
        receipts[name] = {
            "present": True,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return receipts


def result_validity(
    result: dict[str, Any], api_events: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    final = result.get("final_response")
    if not isinstance(final, str) or not final.strip():
        reasons.append("empty-final-response")
    if result.get("partial") is True:
        reasons.append("partial-result")
    if result.get("failed") is True:
        reasons.append("failed-result")
    if result.get("interrupted") is True:
        reasons.append("interrupted-result")
    if result.get("completed") is False:
        reasons.append("not-completed")

    finish_reasons = [
        str(event.get("finish_reason") or "").strip().lower()
        for event in api_events
        if event.get("finish_reason") is not None
    ]
    if not finish_reasons:
        reasons.append("missing-finish-reason")
    elif finish_reasons[-1] in {"length", "max_tokens", "incomplete"}:
        reasons.append(f"truncated-finish:{finish_reasons[-1]}")

    messages = result.get("messages")
    if isinstance(messages, list):
        assistant_finishes = [
            str(message.get("finish_reason") or "").strip().lower()
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("finish_reason") is not None
        ]
        if assistant_finishes and assistant_finishes[-1] in {
            "length",
            "max_tokens",
            "incomplete",
        }:
            reasons.append(f"assistant-truncated:{assistant_finishes[-1]}")
    return not reasons, reasons


def checkpoint_is_resumable(
    payload: dict[str, Any], *, fixture_sha256: str, case_id: str
) -> bool:
    return (
        payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("kind") == KIND
        and payload.get("fixture_sha256") == fixture_sha256
        and payload.get("case_id") == case_id
        and payload.get("valid") is True
        and isinstance(payload.get("output"), str)
        and bool(payload["output"].strip())
    )


def recover_tool_events(state_db: Path, session_id: str) -> list[dict[str, str]]:
    """Recover sanitized tool names from a persisted Hermes transcript."""
    if not state_db.is_file():
        return []
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT role, tool_call_id, tool_calls, tool_name
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()
    finally:
        connection.close()

    names_by_id: dict[str, str] = {}
    events: list[dict[str, str]] = []
    for role, tool_call_id, raw_calls, tool_name in rows:
        if role == "assistant" and raw_calls:
            try:
                calls = json.loads(raw_calls)
            except (TypeError, json.JSONDecodeError):
                calls = []
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    function = call.get("function")
                    name = (
                        str(function.get("name") or "")
                        if isinstance(function, dict)
                        else ""
                    )
                    if call_id and name:
                        names_by_id[call_id] = name
                        events.append({"event": "start", "tool": name})
        elif role == "tool":
            name = str(tool_name or names_by_id.get(str(tool_call_id or "")) or "")
            if name:
                events.append({"event": "complete", "tool": name})
    return events


class EventCapture:
    """Collect sanitized gateway events without persisting prompts or reasoning."""

    def __init__(self) -> None:
        self.api_events: list[dict[str, Any]] = []
        self.tool_events: list[dict[str, Any]] = []
        self.blocked_tool_events: list[dict[str, Any]] = []
        self._manager: Any = None
        self._callback: Any = None
        self._policy_callback: Any = None

    def post_api_request(self, **kwargs: Any) -> None:
        usage = kwargs.get("usage")
        self.api_events.append(
            {
                "model": kwargs.get("model"),
                "response_model": kwargs.get("response_model"),
                "provider": kwargs.get("provider"),
                "api_mode": kwargs.get("api_mode"),
                "base_url_host": _safe_base_url_host(kwargs.get("base_url")),
                "finish_reason": kwargs.get("finish_reason"),
                "duration_ms": round(float(kwargs.get("api_duration") or 0) * 1000),
                "usage": usage if isinstance(usage, dict) else {},
                "assistant_content_chars": kwargs.get("assistant_content_chars"),
                "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
            }
        )

    def tool_start(self, *args: Any, **kwargs: Any) -> None:
        name = kwargs.get("tool_name") or kwargs.get("name")
        if not name and len(args) >= 2:
            name = args[1]
        self.tool_events.append({"event": "start", "tool": str(name or "unknown")})

    def tool_complete(self, *args: Any, **kwargs: Any) -> None:
        name = kwargs.get("tool_name") or kwargs.get("name")
        if not name and len(args) >= 2:
            name = args[1]
        self.tool_events.append({"event": "complete", "tool": str(name or "unknown")})

    def read_only_policy(self, **kwargs: Any) -> dict[str, str] | None:
        tool_name = str(kwargs.get("tool_name") or "")
        args = kwargs.get("args")
        args = args if isinstance(args, dict) else {}
        action = str(args.get("action") or "").strip().lower()
        allowed = tool_name in READ_ONLY_TOOLS or (
            tool_name in READ_ONLY_ACTIONS
            and action in READ_ONLY_ACTIONS[tool_name]
        )
        if allowed:
            return None
        self.blocked_tool_events.append(
            {
                "tool": tool_name or "unknown",
                "action": action or None,
                "reason": "eval-read-only-policy",
            }
        )
        return {
            "action": "block",
            "message": (
                "Read-only evaluation: this tool/action is unavailable because "
                "it could mutate owner state. Continue without changing state."
            ),
        }

    def __enter__(self) -> "EventCapture":
        from hermes_cli.plugins import discover_plugins, get_plugin_manager

        discover_plugins()
        self._manager = get_plugin_manager()
        self._callback = self.post_api_request
        self._policy_callback = self.read_only_policy
        self._manager._hooks.setdefault("post_api_request", []).append(self._callback)
        self._manager._hooks.setdefault("pre_tool_call", []).append(
            self._policy_callback
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        callbacks = self._manager._hooks.get("post_api_request", [])
        self._manager._hooks["post_api_request"] = [
            callback for callback in callbacks if callback is not self._callback
        ]
        callbacks = self._manager._hooks.get("pre_tool_call", [])
        self._manager._hooks["pre_tool_call"] = [
            callback for callback in callbacks if callback is not self._policy_callback
        ]


def _runtime_identity(requested: str, target_model: str) -> dict[str, Any]:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    resolved = resolve_runtime_provider(
        requested=requested,
        target_model=target_model,
    )
    return {
        "requested": requested,
        "target_model": target_model,
        "resolved_model": resolved.get("model"),
        "resolved_provider": resolved.get("provider"),
        "base_url_host": _safe_base_url_host(resolved.get("base_url")),
        "api_mode": resolved.get("api_mode"),
        "entitlement": resolved.get("entitlement"),
        "cost": resolved.get("cost"),
    }


async def run_case(
    *,
    case: dict[str, Any],
    fixture_sha256: str,
    requested: str,
    target_model: str,
    max_tokens: int,
    case_timeout_seconds: int,
    hermes_home: Path,
) -> dict[str, Any]:
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    case_id = safe_case_id(str(case["id"]))
    session_id = f"poke-eval-{fixture_sha256[:12]}-{case_id}"
    gateway_session_key = f"eval:poke-quality:{fixture_sha256[:16]}:{case_id}"
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent_ref: list[Any] = [None]
    runtime_identity = _runtime_identity(requested, target_model)

    started = time.monotonic()
    timed_out = False
    with EventCapture() as capture:
        task = asyncio.create_task(
            adapter._run_agent(
                user_message=str(case["input"]),
                conversation_history=[],
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                toolset_platform="telegram",
                max_tokens=max_tokens,
                agent_ref=agent_ref,
                tool_start_callback=capture.tool_start,
                tool_complete_callback=capture.tool_complete,
            )
        )
        done, _ = await asyncio.wait({task}, timeout=case_timeout_seconds)
        if not done:
            timed_out = True
            agent = agent_ref[0]
            if agent is not None:
                agent.interrupt()
        result, usage = await task
    latency_ms = round((time.monotonic() - started) * 1000)
    agent = agent_ref[0]
    valid, invalid_reasons = result_validity(result, capture.api_events)
    if timed_out:
        valid = False
        invalid_reasons.append(f"case-timeout:{case_timeout_seconds}s")
    prompt = getattr(agent, "_cached_system_prompt", "") or ""
    served_models = [
        str(event.get("response_model") or event.get("model") or "").strip()
        for event in capture.api_events
        if event.get("response_model") or event.get("model")
    ]
    served_model = served_models[-1] if served_models else getattr(agent, "model", None)
    served_source = "provider-response" if any(
        event.get("response_model") for event in capture.api_events
    ) else "agent-runtime"

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "fixture_sha256": fixture_sha256,
        "case_id": case_id,
        "session_id": session_id,
        "input_sha256": sha256_bytes(str(case["input"]).encode("utf-8")),
        "criteria": {
            "deterministic": case.get("deterministic", []),
            "subjective": case.get("subjective", []),
        },
        "valid": valid,
        "invalid_reasons": invalid_reasons,
        "output": result.get("final_response"),
        "result_flags": {
            key: result.get(key)
            for key in ("completed", "partial", "failed", "interrupted", "error")
            if key in result
        },
        "requested": requested,
        "target_model": target_model,
        "served_model": served_model,
        "served_identity_source": served_source,
        "runtime_identity": runtime_identity,
        "agent_identity": {
            "model": getattr(agent, "model", None),
            "provider": getattr(agent, "provider", None),
            "api_mode": getattr(agent, "api_mode", None),
            "base_url_host": _safe_base_url_host(getattr(agent, "base_url", None)),
            "platform": getattr(agent, "platform", None),
            "max_tokens": getattr(agent, "max_tokens", None),
        },
        "context_attestation": {
            "system_prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
            "system_prompt_chars": len(prompt),
            "identity_files": _identity_file_receipts(hermes_home),
            "skip_memory": bool(getattr(agent, "skip_memory", False)),
            "memory_store_loaded": getattr(agent, "_memory_store", None) is not None,
            "memory_manager_loaded": getattr(agent, "_memory_manager", None) is not None,
            "enabled_toolsets": sorted(getattr(agent, "enabled_toolsets", []) or []),
            "gateway_session_key": gateway_session_key,
            "delivery_platform": "api_server",
            "toolset_platform": "telegram",
            "async_delivery": False,
            "ephemeral_system_prompt": False,
            "eval_tool_policy": "read-only-pre-tool-call",
        },
        "api_events": capture.api_events,
        "tool_events": capture.tool_events,
        "blocked_tool_events": capture.blocked_tool_events,
        "usage": usage,
        "latency_ms": latency_ms,
        "max_tokens": max_tokens,
        "case_timeout_seconds": case_timeout_seconds,
        "retry_policy": {"launcher_retries": 0, "automatic_case_retry": False},
    }


async def run(args: argparse.Namespace) -> int:
    from hermes_cli.config import load_config
    from hermes_cli.env_loader import load_hermes_dotenv

    fixture_path = Path(args.fixture).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    hermes_home = Path(
        os.environ.get("HERMES_HOME") or "~/.hermes"
    ).expanduser().resolve()
    load_hermes_dotenv(hermes_home=str(hermes_home))
    config = load_config()
    model_config = config.get("model", {}) if isinstance(config, dict) else {}
    requested = args.requested or str(model_config.get("provider") or "").strip()
    target_model = args.target_model or str(model_config.get("default") or "").strip()
    if not requested or not target_model:
        raise ValueError("requested provider alias and target model must resolve from args/config")

    fixture = load_fixture(fixture_path)
    fixture_sha256 = sha256_file(fixture_path)
    prepare_private_dir(output_dir)
    checkpoints_dir = output_dir / "checkpoints"
    prepare_private_dir(checkpoints_dir)

    selected = fixture["cases"]
    if args.case_id:
        selected = [case for case in selected if case["id"] == args.case_id]
        if len(selected) != 1:
            raise ValueError(f"unknown case id: {args.case_id}")

    results: list[dict[str, Any]] = []
    for case in selected:
        case_id = safe_case_id(str(case["id"]))
        checkpoint = checkpoints_dir / f"{case_id}.json"
        if args.resume and checkpoint.is_file():
            existing = json.loads(checkpoint.read_text(encoding="utf-8"))
            if any(
                str(event.get("tool") or "").startswith("call_")
                for event in existing.get("tool_events", [])
                if isinstance(event, dict)
            ):
                legacy_session_id = str(
                    existing.get("session_id")
                    or f"poke-eval-{fixture_sha256[:12]}-{case_id}"
                )
                recovered = recover_tool_events(
                    hermes_home / "state.db", legacy_session_id
                )
                if recovered:
                    existing["tool_events"] = recovered
                    existing["tool_events_source"] = "session-db-recovery"
                    atomic_private_json(checkpoint, existing)
            if checkpoint_is_resumable(
                existing, fixture_sha256=fixture_sha256, case_id=case_id
            ):
                results.append(existing)
                continue
        row = await run_case(
            case=case,
            fixture_sha256=fixture_sha256,
            requested=requested,
            target_model=target_model,
            max_tokens=args.max_tokens,
            case_timeout_seconds=args.case_timeout_seconds,
            hermes_home=hermes_home,
        )
        atomic_private_json(checkpoint, row)
        results.append(row)
        if not row["valid"]:
            break

    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": (
            "complete"
            if len(results) == len(selected) and all(row["valid"] for row in results)
            else "invalid"
        ),
        "fixture_path": str(fixture_path),
        "fixture_sha256": fixture_sha256,
        "selected_case_count": len(selected),
        "completed_case_count": len(results),
        "valid_case_count": sum(1 for row in results if row["valid"]),
        "requested": requested,
        "target_model": target_model,
        "max_tokens": args.max_tokens,
        "case_timeout_seconds": args.case_timeout_seconds,
        "toolset_platform": "telegram",
        "delivery_platform": "api_server",
        "results": results,
    }
    aggregate_path = output_dir / "receipt.json"
    atomic_private_json(aggregate_path, aggregate)
    print(str(aggregate_path))
    return 0 if aggregate["status"] == "complete" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--requested")
    parser.add_argument("--target-model")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--case-timeout-seconds",
        type=int,
        default=DEFAULT_CASE_TIMEOUT_SECONDS,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_tokens <= 0:
        raise ValueError("--max-tokens must be positive")
    if args.case_timeout_seconds <= 0:
        raise ValueError("--case-timeout-seconds must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
