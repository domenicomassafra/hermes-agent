#!/usr/bin/env python3
"""Hermes client for Signal Deck's typed ``daily.checkin`` contract.

The client intentionally owns no Daily Log state.  It only forwards the
existing Signal Deck contract, so the server remains the sole Registry-gated
Notion writer and source of idempotency.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CHECKIN_PATH = "/api/lifeos/operations/daily.checkin"
ACTOR = "Hermes"
SURFACE = "hermes"


class CheckinError(RuntimeError):
    """An actionable contract or transport failure."""


def _json_file(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckinError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise CheckinError(f"JSON object required: {path}")
    return value


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise CheckinError("base URL must be an HTTPS origin without a path")
    return value.rstrip("/")


def _auth(path: str) -> dict[str, str]:
    value = _json_file(path)
    cookie = value.get("cookie")
    csrf_token = value.get("csrf_token")
    if not isinstance(cookie, str) or not cookie.strip() or not isinstance(csrf_token, str) or not csrf_token.strip():
        raise CheckinError("auth file requires non-empty cookie and csrf_token")
    return {"cookie": cookie, "csrf_token": csrf_token}


def _request(
    base_url: str,
    suffix: str,
    *,
    auth: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Cookie": auth["cookie"],
    }
    if payload is not None:
        headers.update(
            {
                "Content-Type": "application/json",
                "Origin": base_url,
                "X-CSRF-Token": auth["csrf_token"],
            }
        )
    request = urllib.request.Request(f"{base_url}{CHECKIN_PATH}{suffix}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise CheckinError(f"Signal Deck rejected {suffix}: HTTP {exc.code}: {body}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckinError(f"Signal Deck request failed for {suffix}") from exc
    if not isinstance(decoded, dict):
        raise CheckinError("Signal Deck returned a non-object response")
    return decoded


def _checkin_payload(payload_file: str, idempotency_key: str) -> dict[str, Any]:
    payload = _json_file(payload_file)
    if not idempotency_key.strip():
        raise CheckinError("idempotency key is required")
    payload.update(
        {
            "actor": ACTOR,
            "surface": SURFACE,
            "caller_type": "agent",
            "agent_runtime": "hermes",
            "overlay_id": None,
            "idempotency_key": idempotency_key,
        }
    )
    return payload


def preview(base_url: str, auth_file: str, payload_file: str, idempotency_key: str) -> dict[str, Any]:
    return _request(
        _base_url(base_url),
        "/preview",
        auth=_auth(auth_file),
        payload=_checkin_payload(payload_file, idempotency_key),
    )


def apply(
    base_url: str,
    auth_file: str,
    payload_file: str,
    preview_file: str,
    idempotency_key: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != "APPLICA":
        raise CheckinError("apply requires --confirmation APPLICA")
    packet = _json_file(preview_file)
    operation_id = packet.get("operation_id")
    packet_id = packet.get("packet_id")
    if not isinstance(operation_id, str) or not operation_id or not isinstance(packet_id, str) or not packet_id:
        raise CheckinError("preview file lacks operation_id or packet_id")
    payload = _checkin_payload(payload_file, idempotency_key)
    payload.update(
        {
            "operation_id": operation_id,
            "packet_id": packet_id,
            "confirmation": confirmation,
        }
    )
    return _request(_base_url(base_url), "/apply", auth=_auth(auth_file), payload=payload)


def rollback(
    base_url: str,
    auth_file: str,
    operation_id: str,
    idempotency_key: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != "ROLLBACK":
        raise CheckinError("rollback requires --confirmation ROLLBACK")
    if not operation_id or not idempotency_key:
        raise CheckinError("rollback requires operation_id and idempotency_key")
    return _request(
        _base_url(base_url),
        "/rollback",
        auth=_auth(auth_file),
        payload={
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "actor": ACTOR,
            "surface": SURFACE,
            "caller_type": "agent",
            "agent_runtime": "hermes",
            "overlay_id": None,
            "confirmation": confirmation,
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Signal Deck HTTPS origin")
    parser.add_argument("--auth-file", required=True, help="owner-only JSON with cookie and csrf_token")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--payload-file", required=True)
        command.add_argument("--idempotency-key", required=True)
    commands.choices["apply"].add_argument("--preview-file", required=True)
    commands.choices["apply"].add_argument("--confirmation", default="")
    rollback_command = commands.add_parser("rollback")
    rollback_command.add_argument("--operation-id", required=True)
    rollback_command.add_argument("--idempotency-key", required=True)
    rollback_command.add_argument("--confirmation", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preview":
            result = preview(args.base_url, args.auth_file, args.payload_file, args.idempotency_key)
        elif args.command == "apply":
            result = apply(
                args.base_url,
                args.auth_file,
                args.payload_file,
                args.preview_file,
                args.idempotency_key,
                args.confirmation,
            )
        else:
            result = rollback(
                args.base_url,
                args.auth_file,
                args.operation_id,
                args.idempotency_key,
                args.confirmation,
            )
    except CheckinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
