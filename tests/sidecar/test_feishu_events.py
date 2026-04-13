"""Tests for sidecar.feishu_events — Feishu group membership event handling."""

from unittest.mock import AsyncMock

import pytest

from sidecar.feishu_events import FeishuEventHandler

USER_GROUP = "oc_user_group_001"
ADMIN_GROUP = "oc_admin_group_001"


def _make_handler(*, db=None, provisioner=None):
    if db is None:
        db = AsyncMock()
        db.check_event_dedup = AsyncMock(return_value=True)
        db.get_permission = AsyncMock(return_value=None)
    if provisioner is None:
        provisioner = AsyncMock()

    return FeishuEventHandler(
        db=db,
        provisioner=provisioner,
        user_group_chat_id=USER_GROUP,
        admin_group_chat_id=ADMIN_GROUP,
    )


# ── member added ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_added_to_user_group():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_permission = AsyncMock(return_value=None)

    handler = _make_handler(db=db)

    await handler.handle_member_added(
        event_id="evt1", chat_id=USER_GROUP, open_id="ou_alice", name="Alice",
    )

    db.upsert_permission.assert_awaited_once_with(
        "ou_alice", "Alice", is_user_member=True, is_admin=False,
    )
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "permission_granted"


@pytest.mark.asyncio
async def test_user_added_to_user_group_preserves_admin():
    """If user is already an admin, adding to user group keeps is_admin=True."""
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_permission = AsyncMock(return_value={
        "open_id": "ou_bob", "display_name": "Bob",
        "is_user_member": False, "is_admin": True,
    })

    handler = _make_handler(db=db)

    await handler.handle_member_added(
        event_id="evt2", chat_id=USER_GROUP, open_id="ou_bob", name="Bob",
    )

    db.upsert_permission.assert_awaited_once_with(
        "ou_bob", "Bob", is_user_member=True, is_admin=True,
    )


@pytest.mark.asyncio
async def test_user_added_to_admin_group():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_permission = AsyncMock(return_value=None)

    handler = _make_handler(db=db)

    await handler.handle_member_added(
        event_id="evt3", chat_id=ADMIN_GROUP, open_id="ou_carol", name="Carol",
    )

    db.upsert_permission.assert_awaited_once_with(
        "ou_carol", "Carol", is_user_member=False, is_admin=True,
    )
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "admin_granted"


# ── member removed ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_removed_from_user_group():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_permission = AsyncMock(return_value={
        "open_id": "ou_dave", "display_name": "Dave",
        "is_user_member": True, "is_admin": False,
    })

    provisioner = AsyncMock()
    handler = _make_handler(db=db, provisioner=provisioner)

    await handler.handle_member_removed(
        event_id="evt4", chat_id=USER_GROUP, open_id="ou_dave",
    )

    db.upsert_permission.assert_awaited_once_with(
        "ou_dave", "Dave", is_user_member=False, is_admin=False,
    )
    provisioner.suspend_user.assert_awaited_once_with("ou_dave")
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "permission_revoked"


@pytest.mark.asyncio
async def test_user_removed_from_admin_group():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_permission = AsyncMock(return_value={
        "open_id": "ou_eve", "display_name": "Eve",
        "is_user_member": True, "is_admin": True,
    })

    provisioner = AsyncMock()
    handler = _make_handler(db=db, provisioner=provisioner)

    await handler.handle_member_removed(
        event_id="evt5", chat_id=ADMIN_GROUP, open_id="ou_eve",
    )

    db.upsert_permission.assert_awaited_once_with(
        "ou_eve", "Eve", is_user_member=True, is_admin=False,
    )
    provisioner.suspend_user.assert_not_awaited()
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "admin_revoked"


# ── dedup ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_event_ignored():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=False)

    handler = _make_handler(db=db)

    await handler.handle_member_added(
        event_id="evt_dup", chat_id=USER_GROUP, open_id="ou_frank", name="Frank",
    )

    db.upsert_permission.assert_not_awaited()
    db.write_audit.assert_not_awaited()


# ── bot added ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_added_to_group():
    provisioner = AsyncMock()
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)

    handler = _make_handler(db=db, provisioner=provisioner)

    await handler.handle_bot_added(event_id="evt6", chat_id="oc_other_group")

    provisioner.provision_group.assert_awaited_once_with("oc_other_group")


@pytest.mark.asyncio
async def test_bot_added_to_user_group_ignored():
    provisioner = AsyncMock()
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)

    handler = _make_handler(db=db, provisioner=provisioner)

    await handler.handle_bot_added(event_id="evt7", chat_id=USER_GROUP)

    provisioner.provision_group.assert_not_awaited()


# ── group disbanded ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_disbanded():
    db = AsyncMock()
    db.check_event_dedup = AsyncMock(return_value=True)
    db.get_agent_by_chat_id = AsyncMock(return_value={
        "agent_id": "g-acct-chat123", "chat_id": "chat123",
        "status": "active",
    })

    handler = _make_handler(db=db)

    await handler.handle_group_disbanded(event_id="evt8", chat_id="chat123")

    db.update_agent_status.assert_awaited_once_with("g-acct-chat123", "archived")
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "group_disbanded"
