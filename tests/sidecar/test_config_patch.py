"""Tests for sidecar.config_patch — config RPC via openclaw CLI subprocess."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from sidecar.config_patch import ConfigPatchClient, ConfigPatchQueue


@pytest.fixture
def client():
    return ConfigPatchClient(openclaw_bin="openclaw")


# ── Helper to mock subprocess ──────────────────────────────────────

def _mock_subprocess(stdout_data: dict | list, returncode: int = 0):
    """Create a mock for asyncio.create_subprocess_exec."""
    mock_proc = AsyncMock()
    mock_proc.returncode = returncode
    mock_proc.communicate.return_value = (
        json.dumps(stdout_data).encode(),
        b"",
    )
    return mock_proc


# ── ConfigPatchClient ───────────────────────────────────────────────


async def test_get_config():
    """config.get returns normalised {config, baseHash} dict."""
    raw_response = {
        "path": "/fake/openclaw.json",
        "exists": True,
        "parsed": {"agents": {"main": {}}, "bindings": []},
        "hash": "abc123",
    }
    client = ConfigPatchClient()
    with patch("asyncio.create_subprocess_exec", return_value=_mock_subprocess(raw_response)):
        result = await client.get_config()

    assert result["baseHash"] == "abc123"
    assert result["config"]["agents"] == {"main": {}}
    assert result["config"]["bindings"] == []


async def test_patch_config():
    """config.patch sends raw JSON5 string + baseHash."""
    client = ConfigPatchClient()
    with patch("asyncio.create_subprocess_exec", return_value=_mock_subprocess({"ok": True})) as mock_exec:
        await client.patch_config("abc123", '{"agents": {}}')

    # Verify the CLI was called with correct args
    call_args = mock_exec.call_args[0]
    assert "config.patch" in call_args
    assert "--json" in call_args
    assert "--params" in call_args
    params_idx = list(call_args).index("--params")
    params = json.loads(call_args[params_idx + 1])
    assert params["baseHash"] == "abc123"
    assert params["raw"] == '{"agents": {}}'


async def test_add_binding():
    """add_binding GETs current bindings, appends, PATCHes entire array."""
    raw_config = {
        "parsed": {
            "agents": {},
            "bindings": [
                {"agentId": "existing", "match": {"channel": "feishu"}}
            ],
        },
        "hash": "hash1",
    }
    client = ConfigPatchClient()

    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # config.get
            return _mock_subprocess(raw_config)
        else:  # config.patch
            return _mock_subprocess({"ok": True})

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await client.add_binding(
            agent_id="new-agent",
            channel="feishu",
            peer={"kind": "direct", "id": "ou_alice"},
        )

    assert call_count == 2  # get + patch


async def test_remove_binding():
    """remove_binding filters out matching agentId."""
    raw_config = {
        "parsed": {
            "agents": {},
            "bindings": [
                {"agentId": "keep", "match": {"channel": "feishu"}},
                {"agentId": "remove-me", "match": {"channel": "feishu"}},
            ],
        },
        "hash": "hash2",
    }
    client = ConfigPatchClient()

    patches_sent = []

    async def mock_exec(*args, **kwargs):
        if "config.get" in args:
            return _mock_subprocess(raw_config)
        else:
            # Capture the patch params
            params_idx = list(args).index("--params")
            params = json.loads(args[params_idx + 1])
            patches_sent.append(params)
            return _mock_subprocess({"ok": True})

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await client.remove_binding("remove-me")

    assert len(patches_sent) == 1
    patched_bindings = json.loads(patches_sent[0]["raw"])["bindings"]
    assert len(patched_bindings) == 1
    assert patched_bindings[0]["agentId"] == "keep"


async def test_add_agent_with_binding():
    """Atomically adds agent definition + peer binding."""
    raw_config = {
        "parsed": {
            "agents": {"list": [{"id": "old"}]},
            "bindings": [{"agentId": "old", "match": {"channel": "feishu"}}],
        },
        "hash": "hash3",
    }
    client = ConfigPatchClient()

    patches_sent = []

    async def mock_exec(*args, **kwargs):
        if "config.get" in args:
            return _mock_subprocess(raw_config)
        else:
            params_idx = list(args).index("--params")
            params = json.loads(args[params_idx + 1])
            patches_sent.append(params)
            return _mock_subprocess({"ok": True})

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await client.add_agent_with_binding(
            agent_id="u-shared-ou_alice",
            agent_config={"model": "test"},
            channel="feishu",
            peer={"kind": "direct", "id": "ou_alice"},
        )

    assert len(patches_sent) == 1
    patch_data = json.loads(patches_sent[0]["raw"])
    # agents.list should have 2 entries (old + new)
    agents_list = patch_data["agents"]["list"]
    assert len(agents_list) == 2
    assert agents_list[1]["id"] == "u-shared-ou_alice"
    # bindings should have 2 entries; new binding inserted before fallback catch-all
    assert len(patch_data["bindings"]) == 2
    new_binding = patch_data["bindings"][0]  # inserted before catch-all
    assert new_binding["agentId"] == "u-shared-ou_alice"
    assert new_binding["match"]["peer"]["kind"] == "direct"
    # catch-all remains last
    assert patch_data["bindings"][1]["agentId"] == "old"


# ── ConfigPatchQueue ────────────────────────────────────────────────


async def test_queue_flush_merges_operations():
    """Queue merges multiple operations into a single config.patch call."""
    mock_client = AsyncMock(spec=ConfigPatchClient)
    mock_client.get_config.return_value = {
        "baseHash": "hash-q",
        "config": {"agents": {}, "bindings": []},
    }
    mock_client.patch_config.return_value = {"ok": True}

    queue = ConfigPatchQueue(mock_client)
    await queue.enqueue({"add_agent": {"name": "A1"}, "agent_id": "agent-1"})
    await queue.enqueue({"add_binding": {"agentId": "agent-1", "match": {"channel": "feishu"}}})
    await queue.enqueue({"remove_binding_agent_id": "old-agent"})

    await queue.flush_now()

    mock_client.get_config.assert_called_once()
    mock_client.patch_config.assert_called_once()


async def test_queue_retry_on_conflict():
    """Queue retries up to MAX_RETRIES on conflict."""
    mock_client = AsyncMock(spec=ConfigPatchClient)
    mock_client.get_config.return_value = {
        "baseHash": "hash-retry",
        "config": {"agents": {}, "bindings": []},
    }
    mock_client.patch_config.side_effect = [
        Exception("conflict"),
        Exception("conflict"),
        {"ok": True},
    ]

    queue = ConfigPatchQueue(mock_client)
    await queue.enqueue({"add_agent": {"name": "A1"}, "agent_id": "agent-1"})
    await queue.flush_now()

    assert mock_client.patch_config.call_count == 3


async def test_queue_drops_batch_after_max_retries():
    """Queue drops batch after MAX_RETRIES failures."""
    mock_client = AsyncMock(spec=ConfigPatchClient)
    mock_client.get_config.return_value = {
        "baseHash": "hash-fail",
        "config": {"agents": {}, "bindings": []},
    }
    mock_client.patch_config.side_effect = Exception("always fails")

    queue = ConfigPatchQueue(mock_client)
    await queue.enqueue({"add_agent": {"name": "A1"}, "agent_id": "agent-1"})
    await queue.flush_now()  # should not raise

    assert mock_client.patch_config.call_count == ConfigPatchQueue.MAX_RETRIES
