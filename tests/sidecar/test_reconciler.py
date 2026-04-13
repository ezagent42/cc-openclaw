"""Tests for sidecar.reconciler — periodic Feishu group membership sync."""

from unittest.mock import AsyncMock, call

import pytest

from sidecar.reconciler import FeishuGroupAPI, Reconciler


def _make_reconciler(*, db=None, provisioner=None, feishu_api=None):
    if db is None:
        db = AsyncMock()
    if provisioner is None:
        provisioner = AsyncMock()
    if feishu_api is None:
        feishu_api = AsyncMock(spec=FeishuGroupAPI)

    r = Reconciler(
        db=db,
        provisioner=provisioner,
        feishu_api=feishu_api,
        user_group_chat_id="oc_user_group",
        admin_group_chat_id="oc_admin_group",
    )
    return r, db, provisioner, feishu_api


# ── test_reconcile_grants_missing_permission ─────────────────────────


async def test_reconcile_grants_missing_permission():
    """User present in Feishu user group but absent from DB -> upsert + audit."""
    r, db, provisioner, feishu_api = _make_reconciler()

    # Feishu says Alice is in the user group, nobody in admin group
    feishu_api.get_group_members.side_effect = lambda chat_id: {
        "oc_user_group": [{"open_id": "ou_alice", "name": "Alice"}],
        "oc_admin_group": [],
    }[chat_id]

    # DB has no permissions at all
    db.list_all_permissions.return_value = []

    await r.reconcile()

    # Should grant Alice as user member (not admin)
    db.upsert_permission.assert_any_await(
        "ou_alice", "Alice", is_user_member=True, is_admin=False,
    )

    # Audit log written
    audit_calls = db.write_audit.call_args_list
    grant_calls = [c for c in audit_calls if c[0][0] == "reconciled_grant"]
    assert len(grant_calls) >= 1
    assert "ou_alice" in grant_calls[0][0][1]

    # No suspensions
    provisioner.suspend_user.assert_not_awaited()


# ── test_reconcile_revokes_stale_permission ──────────────────────────


async def test_reconcile_revokes_stale_permission():
    """User in DB permission table but not in any Feishu group -> revoke + suspend."""
    r, db, provisioner, feishu_api = _make_reconciler()

    # Both Feishu groups are empty
    feishu_api.get_group_members.return_value = []

    # DB has Bob with user membership
    db.list_all_permissions.return_value = [
        {
            "open_id": "ou_bob",
            "display_name": "Bob",
            "is_user_member": True,
            "is_admin": False,
        }
    ]

    await r.reconcile()

    # Should revoke: upsert with both flags False
    db.upsert_permission.assert_any_await(
        "ou_bob", "Bob", is_user_member=False, is_admin=False,
    )

    # Should suspend because Bob was a user member
    provisioner.suspend_user.assert_awaited_once_with("ou_bob")

    # Audit log written
    audit_calls = db.write_audit.call_args_list
    revoke_calls = [c for c in audit_calls if c[0][0] == "reconciled_revoke"]
    assert len(revoke_calls) >= 1
    assert "ou_bob" in revoke_calls[0][0][1]

    # Cleanup old events called
    db.cleanup_old_events.assert_awaited_once()


# ── test_reconcile_fixes_inconsistent_flags ──────────────────────────


async def test_reconcile_fixes_inconsistent_flags():
    """User in admin group but DB shows is_admin=False -> fix flags."""
    r, db, provisioner, feishu_api = _make_reconciler()

    # Carol is in admin group (and also user group)
    feishu_api.get_group_members.side_effect = lambda chat_id: {
        "oc_user_group": [{"open_id": "ou_carol", "name": "Carol"}],
        "oc_admin_group": [{"open_id": "ou_carol", "name": "Carol"}],
    }[chat_id]

    # DB has Carol as user but not admin
    db.list_all_permissions.return_value = [
        {
            "open_id": "ou_carol",
            "display_name": "Carol",
            "is_user_member": True,
            "is_admin": False,
        }
    ]

    await r.reconcile()

    # Should fix: upsert with is_admin=True
    db.upsert_permission.assert_any_await(
        "ou_carol", "Carol", is_user_member=True, is_admin=True,
    )

    # No suspensions (Carol is still in a group)
    provisioner.suspend_user.assert_not_awaited()

    # Audit log for the grant/fix
    audit_calls = db.write_audit.call_args_list
    grant_calls = [c for c in audit_calls if c[0][0] == "reconciled_grant"]
    assert len(grant_calls) >= 1
