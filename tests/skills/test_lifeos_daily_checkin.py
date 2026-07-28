from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.request
import urllib.response
from email.message import Message
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


class StubHTTPSHandler(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, status, *, location=None, body=b'{"error":"bad request"}'):
        self.status = status
        self.location = location
        self.body = body
        self.requests = []
        self.responses = []

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        if self.location is not None:
            headers["Location"] = self.location
        response = urllib.response.addinfourl(
            io.BytesIO(self.body),
            headers,
            request.full_url,
            self.status,
        )
        response.msg = "stub response"
        self.responses.append(response)
        return response


def write_inputs(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"cookie": "session=abc", "csrf_token": "csrf"}))
    payload = tmp_path / "checkin.json"
    payload.write_text(json.dumps({"date": "2026-07-28", "mood": 8}))
    return auth, payload


def test_preview_uses_the_shared_actor_surface_and_key(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    with patch.object(
        mod._OPENER,
        "open",
        return_value=Response({"packet_id": "packet", "operation_id": "operation"}),
    ) as open_request:
        result = mod.preview("https://deck.example", str(auth), str(payload), "shared-key")

    request = open_request.call_args.args[0]
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


def test_http_origin_is_rejected_before_credentials_are_sent(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)

    with patch.object(mod._OPENER, "open") as open_request, pytest.raises(
        mod.CheckinError, match="HTTPS"
    ):
        mod.preview("http://127.0.0.1:8443", str(auth), str(payload), "shared-key")

    open_request.assert_not_called()


def test_apply_reuses_preview_identity_key_and_requires_exact_confirmation(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    preview = tmp_path / "preview.json"
    preview.write_text(json.dumps({"packet_id": "packet", "operation_id": "operation"}))

    with pytest.raises(mod.CheckinError, match="APPLICA"):
        mod.apply("https://deck.example", str(auth), str(payload), str(preview), "shared-key", "confirm")

    with patch.object(
        mod._OPENER,
        "open",
        return_value=Response({"status": "applied", "mutation_count": 1}),
    ) as open_request:
        result = mod.apply("https://deck.example", str(auth), str(payload), str(preview), "shared-key", "APPLICA")

    request = open_request.call_args.args[0]
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

    with patch.object(
        mod._OPENER,
        "open",
        return_value=Response({"status": "rolled-back"}),
    ) as open_request:
        result = mod.rollback("https://deck.example", str(auth), "operation", "shared-key", "ROLLBACK")

    body = json.loads(open_request.call_args.args[0].data)
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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    [
        "http://deck.example/insecure",
        "https://other.example/cross-origin",
        "https://deck.example/same-origin",
        "/relative",
    ],
    ids=["https-to-http", "cross-origin", "same-origin", "relative"],
)
def test_redirects_are_rejected_without_forwarding_credentials(tmp_path, status, location):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    transport = StubHTTPSHandler(status, location=location)
    mod._OPENER.add_handler(transport)

    with patch.object(mod.urllib.request, "urlopen") as default_urlopen, pytest.raises(
        mod.CheckinError, match=rf"redirect rejected: HTTP {status}"
    ):
        mod.preview("https://deck.example", str(auth), str(payload), "shared-key")

    assert len(transport.requests) == 1
    request = transport.requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.full_url.endswith("/daily.checkin/preview")
    assert headers["cookie"] == "session=abc"
    assert headers["x-csrf-token"] == "csrf"
    assert transport.responses[0].closed
    default_urlopen.assert_not_called()


def test_http_error_processor_preserves_400_behavior(tmp_path):
    mod = load_module()
    auth, payload = write_inputs(tmp_path)
    transport = StubHTTPSHandler(400)
    mod._OPENER.add_handler(transport)

    assert any(
        isinstance(handler, urllib.request.HTTPErrorProcessor)
        for handler in mod._OPENER.handlers
    )
    with patch.object(mod.urllib.request, "urlopen") as default_urlopen, pytest.raises(
        mod.CheckinError,
        match=r"Signal Deck rejected /preview: HTTP 400: \{\"error\":\"bad request\"\}",
    ):
        mod.preview("https://deck.example", str(auth), str(payload), "shared-key")

    assert len(transport.requests) == 1
    default_urlopen.assert_not_called()
