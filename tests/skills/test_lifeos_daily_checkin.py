from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "productivity"
    / "lifeos"
    / "scripts"
    / "daily_checkin.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("lifeos_daily_checkin", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def write_inputs(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"cookie": "session=abc", "csrf_token": "csrf"}))
    payload = tmp_path / "checkin.json"
    payload.write_text(json.dumps({"date": "2026-07-28", "mood": 8}))
    return auth, payload


def test_preview_uses_the_shared_actor_surface_and_key(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    with patch.object(mod.urllib.request, "urlopen", return_value=Response({"packet_id": "packet", "operation_id": "operation"})) as urlopen:
        result = mod.preview("https://deck.example", str(auth), str(payload), "shared-key")

    request = urlopen.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/daily.checkin/preview")
    assert body["actor"] == "Hermes"
    assert body["surface"] == "hermes"
    assert body["caller_type"] == "agent"
    assert body["agent_runtime"] == "hermes"
    assert body["overlay_id"] is None
    assert body["idempotency_key"] == "shared-key"
    assert "confirmation" not in body
    assert result["operation_id"] == "operation"


def test_apply_reuses_preview_identity_key_and_requires_exact_confirmation(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    preview = tmp_path / "preview.json"
    preview.write_text(json.dumps({"packet_id": "packet", "operation_id": "operation"}))

    with pytest.raises(mod.CheckinError, match="APPLICA"):
        mod.apply("https://deck.example", str(auth), str(payload), str(preview), "shared-key", "confirm")

    with patch.object(mod.urllib.request, "urlopen", return_value=Response({"status": "applied", "mutation_count": 1})) as urlopen:
        result = mod.apply("https://deck.example", str(auth), str(payload), str(preview), "shared-key", "APPLICA")

    request = urlopen.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/daily.checkin/apply")
    assert body["operation_id"] == "operation"
    assert body["packet_id"] == "packet"
    assert body["idempotency_key"] == "shared-key"
    assert body["confirmation"] == "APPLICA"
    assert result["mutation_count"] == 1


def test_rollback_requires_owner_confirmation_and_preserves_actor_identity(tmp_path):
    mod = load_module()
    auth, _ = write_inputs(tmp_path)
    with pytest.raises(mod.CheckinError, match="ROLLBACK"):
        mod.rollback("https://deck.example", str(auth), "operation", "shared-key", "rollback")

    with patch.object(mod.urllib.request, "urlopen", return_value=Response({"status": "rolled-back"})) as urlopen:
        result = mod.rollback("https://deck.example", str(auth), "operation", "shared-key", "ROLLBACK")

    body = json.loads(urlopen.call_args.args[0].data)
    assert body == {
        "operation_id": "operation",
        "idempotency_key": "shared-key",
        "actor": "Hermes",
        "surface": "hermes",
        "caller_type": "agent",
        "agent_runtime": "hermes",
        "overlay_id": None,
        "confirmation": "ROLLBACK",
    }
    assert result["status"] == "rolled-back"
