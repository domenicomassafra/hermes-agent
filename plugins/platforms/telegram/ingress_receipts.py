"""Persistent, redacted Telegram ingress decisions.

This module deliberately owns only the narrow pre-session boundary. It never
stores message text, captions, usernames, or provider data: it records a
stable transport key plus hashes of each transport identity and the admission
decision. The transport key makes accepted ingress at-most-once across a
gateway restart; rejected messages remain unable to reach session/model/tool
handling.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_ingress_receipts (
    dedupe_key TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL,
    first_decision TEXT NOT NULL,
    latest_decision TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0
)
"""


def _identity_hash(value: object) -> str | None:
    """Return a deterministic redaction hash without retaining raw identity."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_redacted_receipt(
    *,
    chat_id: object,
    topic_id: object,
    message_id: object,
    sender_class: str,
    is_bot: bool,
    forwarded: bool,
    decision: str,
) -> dict[str, Any]:
    """Build a bounded receipt that contains transport provenance, not content."""
    return {
        "schema": "telegram-ingress-receipt.v1",
        "chat_id_sha256": _identity_hash(chat_id),
        "topic_id_sha256": _identity_hash(topic_id),
        "message_id_sha256": _identity_hash(message_id),
        "sender_class": sender_class,
        "is_bot": bool(is_bot),
        "forwarded": bool(forwarded),
        "decision": decision,
    }


def build_invalid_identity_key(
    *, chat_id: object, topic_id: object, message_id: object
) -> str:
    """Return a non-content key for a malformed transport identity.

    Valid updates always use the externally documented
    ``telegram:<chat>:<topic>:<message_id>`` dedupe key. A malformed update
    cannot safely claim that identity, but it still needs a durable redacted
    rejection receipt before the adapter returns.
    """
    material = "\x1f".join("" if value is None else str(value) for value in (chat_id, topic_id, message_id))
    return "telegram:invalid:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class TelegramIngressReceiptStore:
    """A tiny durable receipt ledger scoped to the active Hermes profile."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_hermes_home() / "state" / "telegram_ingress_receipts.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=1.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute(_SCHEMA)
        return conn

    def record_once(self, dedupe_key: str, receipt: Mapping[str, Any]) -> bool:
        """Persist one first-seen decision; return ``False`` for a replay.

        A duplicate updates only bounded replay metadata. The original receipt
        remains intact so the audit trail cannot claim that a replay was the
        first disposition of the Telegram message.
        """
        now = time.time()
        encoded = json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT dedupe_key FROM telegram_ingress_receipts WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE telegram_ingress_receipts "
                    "SET latest_decision = ?, updated_at = ?, replay_count = replay_count + 1 "
                    "WHERE dedupe_key = ?",
                    ("replay_rejected", now, dedupe_key),
                )
                conn.execute("COMMIT")
                return False
            decision = str(receipt.get("decision", "rejected"))
            conn.execute(
                "INSERT INTO telegram_ingress_receipts "
                "(dedupe_key, receipt_json, first_decision, latest_decision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dedupe_key, encoded, decision, decision, now, now),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def receipt_for(self, dedupe_key: str) -> dict[str, Any] | None:
        """Test/diagnostic read of the redacted first decision only."""
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT receipt_json FROM telegram_ingress_receipts WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            return json.loads(row[0]) if row is not None else None
        finally:
            conn.close()
