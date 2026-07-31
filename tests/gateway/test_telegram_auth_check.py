"""Tests for Telegram adapter early authorization check.

Verifies that unauthorized users are blocked before any text batching,
event building, or response generation occurs.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.telegram.ingress_receipts import (
    TelegramIngressReceiptStore,
    build_invalid_identity_key,
)


def _make_adapter(allow_from=None, allowed_chats=None, group_allowed_chats=None, callback_auth=None, **extra_overrides):
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except ModuleNotFoundError:  # PR branch before Telegram plugin extraction
        from gateway.platforms.telegram import TelegramAdapter

    extra = {}
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if group_allowed_chats is not None:
        extra["group_allowed_chats"] = group_allowed_chats
    extra.update(extra_overrides)

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._dm_topics = {}
    adapter._dm_topics_config = []
    adapter._dm_topic_chat_ids = set()
    if callback_auth is not None:
        adapter._is_callback_user_authorized = callback_auth
    return adapter


def _make_message(
    text="hello",
    *,
    from_user_id=111,
    chat_id=-100,
    chat_type="group",
    message_id=42,
    topic_id=None,
    is_bot=False,
    forwarded=False,
):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=topic_id,
        is_topic_message=topic_id is not None,
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Test", is_forum=topic_id is not None),
        from_user=SimpleNamespace(id=from_user_id, full_name="Test User", first_name="Test", is_bot=is_bot),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
        forward_origin=SimpleNamespace() if forwarded else None,
        is_automatic_forward=False,
        forward_date=None,
        forward_from=None,
        forward_from_chat=None,
        forward_sender_name=None,
    )


@pytest.mark.asyncio
async def test_unauthorized_user_blocked_before_event_building():
    """Unauthorized user's message should be blocked before _build_message_event."""
    adapter = _make_adapter(group_allow_from=["222"])  # Only user 222 allowed in groups

    build_called = False
    original_build = adapter._build_message_event

    def track_build(*a, **kw):
        nonlocal build_called
        build_called = True
        return original_build(*a, **kw)

    adapter._build_message_event = track_build

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(from_user_id=111, chat_type="group"),  # User 111 NOT in group_allow_from
        effective_message=None,
    )

    await adapter._handle_text_message(update, SimpleNamespace())

    assert build_called is False, "build_message_event should not be called for unauthorized user"


@pytest.mark.asyncio
async def test_authorized_user_processed_normally():
    """Authorized user's message should pass the auth check and build an event."""
    adapter = _make_adapter(group_allow_from=["111"])

    build_called = False
    original_build = adapter._build_message_event

    def track_build(*a, **kw):
        nonlocal build_called
        build_called = True
        return original_build(*a, **kw)

    adapter._build_message_event = track_build

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(from_user_id=111, chat_type="group"),
        effective_message=None,
    )

    await adapter._handle_text_message(update, SimpleNamespace())

    assert build_called is True, "build_message_event should be called for authorized user"


@pytest.mark.asyncio
async def test_channel_post_passes_auth():
    """Messages with no from_user (channel posts) should pass user-level auth."""
    adapter = _make_adapter(allow_from=["111"])

    build_called = False
    original_build = adapter._build_message_event

    def track_build(*a, **kw):
        nonlocal build_called
        build_called = True
        return original_build(*a, **kw)

    adapter._build_message_event = track_build

    msg = _make_message()
    msg.from_user = None  # Channel post has no sender

    update = SimpleNamespace(
        update_id=1,
        message=msg,
        effective_message=None,
    )

    await adapter._handle_text_message(update, SimpleNamespace())

    assert build_called is True, "Channel posts should pass user-level auth"


@pytest.mark.asyncio
async def test_command_from_unauthorized_user_blocked():
    """Commands from unauthorized users should be blocked."""
    adapter = _make_adapter(group_allow_from=["222"])
    adapter.handle_message = AsyncMock()

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(text="/start", from_user_id=111, chat_type="group"),
        effective_message=None,
    )

    await adapter._handle_command(update, SimpleNamespace())

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_from_authorized_user_processed():
    """Commands from authorized users should be processed."""
    adapter = _make_adapter(group_allow_from=["111"])
    adapter.handle_message = AsyncMock()

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(text="/start", from_user_id=111, chat_type="group"),
        effective_message=None,
    )

    await adapter._handle_command(update, SimpleNamespace())

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_location_from_unauthorized_user_blocked():
    """Location messages from unauthorized users should be blocked."""
    adapter = _make_adapter(group_allow_from=["222"])

    msg = _make_message(from_user_id=111, chat_type="group")
    msg.text = None
    msg.location = SimpleNamespace(latitude=53.3498, longitude=-6.2603)

    update = SimpleNamespace(
        update_id=1,
        message=msg,
        effective_message=None,
    )

    # Should not raise — just silently return
    await adapter._handle_location_message(update, SimpleNamespace())


def test_is_user_authorized_from_message_allow_from():
    """_is_user_authorized_from_message should respect adapter-level allow_from for DMs."""
    adapter = _make_adapter(allow_from=["111", "222"])

    msg = _make_message(from_user_id=111, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is True

    msg = _make_message(from_user_id=333, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_is_user_authorized_from_message_group_allow_from():
    """_is_user_authorized_from_message should respect adapter-level group_allow_from for groups."""
    adapter = _make_adapter(group_allow_from=["111", "222"])

    msg = _make_message(from_user_id=111, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is True

    msg = _make_message(from_user_id=333, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_is_user_authorized_from_message_wildcard():
    """_is_user_authorized_from_message should accept wildcard '*'."""
    adapter = _make_adapter(allow_from=["*"])

    msg = _make_message(from_user_id=999)
    assert adapter._is_user_authorized_from_message(msg) is True


def test_is_user_authorized_from_message_no_from_user():
    """_is_user_authorized_from_message should return True for messages without from_user."""
    adapter = _make_adapter(allow_from=["111"])

    msg = _make_message()
    msg.from_user = None
    assert adapter._is_user_authorized_from_message(msg) is True


def test_is_user_authorized_from_message_callback():
    """_is_user_authorized_from_message should use _is_callback_user_authorized."""
    adapter = _make_adapter(callback_auth=lambda uid, **_kw: uid == "555")

    msg = _make_message(from_user_id=555)
    assert adapter._is_user_authorized_from_message(msg) is True

    msg = _make_message(from_user_id=666)
    assert adapter._is_user_authorized_from_message(msg) is False


def test_unknown_dm_with_no_allowlist_passes_to_pairing(monkeypatch):
    """Unknown DMs must still reach the gateway pairing flow when no allowlist exists."""
    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    adapter = _make_adapter()
    msg = _make_message(from_user_id=111, chat_id=111, chat_type="private")

    assert adapter._is_user_authorized_from_message(msg) is True


def test_runner_auth_gets_group_user_allowlist_context(monkeypatch):
    """Group user allowlists need a group-shaped source, not a DM-shaped one."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "111")
    seen_sources = []

    class Runner:
        def _is_user_authorized(self, source):
            seen_sources.append(source)
            return source.chat_type == "group" and source.chat_id == "-100" and source.user_id == "111"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=111, chat_id=-100, chat_type="group")

    assert adapter._is_user_authorized_from_message(msg) is True
    assert seen_sources
    assert seen_sources[0].chat_type == "group"
    assert seen_sources[0].chat_id == "-100"


def test_runner_auth_gets_group_chat_allowlist_context(monkeypatch):
    """Group chat allowlists need the real chat id before intake drops updates."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-222")
    seen_sources = []

    class Runner:
        def _is_user_authorized(self, source):
            seen_sources.append(source)
            return source.chat_type == "group" and source.chat_id == "-222"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=111, chat_id=-222, chat_type="group")

    assert adapter._is_user_authorized_from_message(msg) is True
    assert seen_sources
    assert seen_sources[0].chat_type == "group"
    assert seen_sources[0].chat_id == "-222"


def test_removed_dm_user_blocked_before_pairing_when_allowlist_exists(monkeypatch):
    """A user removed from TELEGRAM_ALLOWED_USERS should be blocked at intake."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "222")
    adapter = _make_adapter()
    msg = _make_message(from_user_id=111, chat_id=111, chat_type="private")

    assert adapter._is_user_authorized_from_message(msg) is False


@pytest.mark.asyncio
async def test_media_from_removed_user_blocked_before_event_building(monkeypatch):
    """Removed users must not inject prompt-bearing documents via media handlers."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "222")
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()

    build_called = False

    def track_build(*_args, **_kwargs):
        nonlocal build_called
        build_called = True
        raise AssertionError("media handler built an event for an unauthorized user")

    adapter._build_message_event = track_build
    document = SimpleNamespace(
        file_name="payload.txt",
        mime_type="text/plain",
        file_size=42,
        get_file=AsyncMock(side_effect=AssertionError("unauthorized document was downloaded")),
    )
    msg = _make_message(text=None, from_user_id=111, chat_id=111, chat_type="private")
    msg.caption = "please process this caption"
    msg.document = document

    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_media_message(update, SimpleNamespace())

    assert build_called is False
    adapter.handle_message.assert_not_awaited()
    document.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmentioned_group_text_from_removed_user_not_observed():
    """Removed users must not persist unmentioned group text into observed context."""
    adapter = _make_adapter(
        group_allow_from=["222"],
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        require_mention=True,
        observe_unmentioned_group_messages=True,
    )
    observed = []
    adapter._observe_unmentioned_group_message = lambda *args, **kwargs: observed.append((args, kwargs))

    msg = _make_message(text="side chatter", from_user_id=111, chat_id=-100, chat_type="group")
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_text_message(update, SimpleNamespace())

    assert observed == []


@pytest.mark.asyncio
async def test_unmentioned_group_location_from_removed_user_not_observed():
    """Removed users must not persist unmentioned group locations into observed context."""
    adapter = _make_adapter(
        group_allow_from=["222"],
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        require_mention=True,
        observe_unmentioned_group_messages=True,
    )
    observed = []
    adapter._observe_unmentioned_group_message = lambda *args, **kwargs: observed.append((args, kwargs))

    msg = _make_message(text=None, from_user_id=111, chat_id=-100, chat_type="group")
    msg.location = SimpleNamespace(latitude=53.3498, longitude=-6.2603)
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_location_message(update, SimpleNamespace())

    assert observed == []


def _make_signor_rivendita_adapter(tmp_path):
    """Build the strict profile boundary with an isolated durable ledger."""
    adapter = _make_adapter(
        profile_name="signorrivendita",
        group_allow_from=["111"],
        allowed_chats=["-100"],
        allowed_topics=["15"],
        free_response_topics=["-100:15"],
        require_mention=False,
    )
    adapter._signor_rivendita_ingress_store = TelegramIngressReceiptStore(
        tmp_path / "state" / "telegram_ingress_receipts.sqlite3"
    )
    adapter._enqueue_text_event = Mock()
    return adapter


def _signor_update(message):
    return SimpleNamespace(update_id=41, message=message, effective_message=None)


@pytest.mark.asyncio
async def test_signor_forward_is_rejected_before_session_model_tool_or_notion(tmp_path):
    adapter = _make_signor_rivendita_adapter(tmp_path)
    adapter._session_store = Mock()
    adapter._provider_call = Mock()
    adapter._tool_call = Mock()
    adapter._notion_mutation = Mock()
    adapter._build_message_event = Mock(side_effect=AssertionError("forward reached session path"))

    forwarded = _make_message(
        chat_id=-100, chat_type="supergroup", topic_id=15, forwarded=True
    )
    await adapter._handle_text_message(_signor_update(forwarded), SimpleNamespace())

    adapter._build_message_event.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()
    adapter._session_store.assert_not_called()
    adapter._provider_call.assert_not_called()
    adapter._tool_call.assert_not_called()
    adapter._notion_mutation.assert_not_called()


@pytest.mark.asyncio
async def test_signor_bot_sender_is_rejected_before_event_building(tmp_path):
    adapter = _make_signor_rivendita_adapter(tmp_path)
    adapter._build_message_event = Mock(side_effect=AssertionError("bot reached event build"))

    bot_message = _make_message(
        chat_id=-100, chat_type="supergroup", topic_id=15, is_bot=True
    )
    await adapter._handle_text_message(_signor_update(bot_message), SimpleNamespace())

    adapter._build_message_event.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_signor_replay_persists_across_adapter_restart(tmp_path):
    first = _make_signor_rivendita_adapter(tmp_path)
    message = _make_message(chat_id=-100, chat_type="supergroup", topic_id=15, message_id=77)

    await first._handle_text_message(_signor_update(message), SimpleNamespace())
    first._enqueue_text_event.assert_called_once()

    restarted = _make_signor_rivendita_adapter(tmp_path)
    await restarted._handle_text_message(_signor_update(message), SimpleNamespace())

    restarted._enqueue_text_event.assert_not_called()
    assert restarted._signor_rivendita_ingress_store.receipt_for("telegram:-100:15:77") is not None


@pytest.mark.asyncio
async def test_signor_direct_owner_in_exact_topic_is_processed(tmp_path):
    adapter = _make_signor_rivendita_adapter(tmp_path)
    direct_owner = _make_message(chat_id=-100, chat_type="supergroup", topic_id=15, message_id=88)

    await adapter._handle_text_message(_signor_update(direct_owner), SimpleNamespace())

    adapter._enqueue_text_event.assert_called_once()


@pytest.mark.asyncio
async def test_signor_direct_owner_uses_existing_scout_topic_binding(tmp_path, monkeypatch):
    """The existing worker Scan topic may bind the owner's direct-message lane."""
    monkeypatch.setenv("TELEGRAM_SCAN_TOPIC_ID", "15")
    adapter = _make_adapter(
        profile_name="signorrivendita",
        allow_from=["111"],
        require_mention=False,
    )
    adapter._signor_rivendita_ingress_store = TelegramIngressReceiptStore(
        tmp_path / "state" / "telegram_ingress_receipts.sqlite3"
    )
    adapter._enqueue_text_event = Mock()
    direct_owner = _make_message(
        chat_id=111, chat_type="private", topic_id=15, message_id=91
    )

    await adapter._handle_text_message(_signor_update(direct_owner), SimpleNamespace())

    adapter._enqueue_text_event.assert_called_once()


@pytest.mark.asyncio
async def test_signor_missing_provenance_and_topic_mismatch_fail_closed(tmp_path):
    adapter = _make_signor_rivendita_adapter(tmp_path)
    missing_message_id = _make_message(chat_id=-100, chat_type="supergroup", topic_id=15, message_id=None)
    wrong_topic = _make_message(chat_id=-100, chat_type="supergroup", topic_id=16, message_id=89)

    await adapter._handle_text_message(_signor_update(missing_message_id), SimpleNamespace())
    await adapter._handle_text_message(_signor_update(wrong_topic), SimpleNamespace())

    adapter._enqueue_text_event.assert_not_called()
    missing_receipt = adapter._signor_rivendita_ingress_store.receipt_for(
        build_invalid_identity_key(chat_id="-100", topic_id="15", message_id=None)
    )
    assert missing_receipt["decision"] == "rejected_missing_provenance"


def test_signor_ingress_receipt_is_redacted_and_has_stable_transport_key(tmp_path):
    adapter = _make_signor_rivendita_adapter(tmp_path)
    message = _make_message(chat_id=-100, chat_type="supergroup", topic_id=15, message_id=90)
    message.caption = "owner@example.test private caption"

    assert adapter._admit_signor_rivendita_ingress(message) is True
    receipt = adapter._signor_rivendita_ingress_store.receipt_for("telegram:-100:15:90")

    assert receipt["schema"] == "telegram-ingress-receipt.v1"
    assert receipt["sender_class"] == "user"
    assert receipt["is_bot"] is False
    assert receipt["forwarded"] is False
    assert receipt["decision"] == "accepted"
    assert all(len(receipt[key]) == 64 for key in (
        "chat_id_sha256", "topic_id_sha256", "message_id_sha256",
    ))
    assert "-100" not in json.dumps(receipt)
    assert "owner@example.test" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_other_profiles_keep_existing_forward_behavior(tmp_path):
    adapter = _make_adapter(profile_name="other-profile", require_mention=False)
    adapter._enqueue_text_event = Mock()
    forwarded = _make_message(forwarded=True)

    await adapter._handle_text_message(_signor_update(forwarded), SimpleNamespace())

    adapter._enqueue_text_event.assert_called_once()
