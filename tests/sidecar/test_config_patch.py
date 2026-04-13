"""Tests for sidecar.config_patch — config.get / config.patch RPC client."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from sidecar.config_patch import ConfigPatchClient, ConfigPatchQueue


GATEWAY_URL = "http://127.0.0.1:18789"
AUTH_TOKEN = "test-token-123"


@pytest.fixture
def client():
    return ConfigPatchClient(gateway_url=GATEWAY_URL, auth_token=AUTH_TOKEN)


# ── ConfigPatchClient ───────────────────────────────────────────────


async def test_get_config(client):
    """Verifies POST to /api/config.get returns baseHash and config."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "baseHash": "abc123",
        "config": {"agents": {}, "bindings": []},
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        result = await client.get_config()

    mock_post.assert_called_once_with(
        f"{GATEWAY_URL}/api/config.get",
        json={},
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    assert result["baseHash"] == "abc123"
    assert result["config"]["bindings"] == []


async def test_patch_config(client):
    """Verifies POST to /api/config.patch includes baseHash."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}
    mock_response.raise_for_status = MagicMock()

    patch_data = {"agents": {"agent-1": {"name": "Test"}}}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        result = await client.patch_config("abc123", patch_data)

    mock_post.assert_called_once_with(
        f"{GATEWAY_URL}/api/config.patch",
        json={"baseHash": "abc123", "patch": patch_data},
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    assert result["ok"] is True


async def test_add_binding(client):
    """Verifies GET + append + PATCH flow (bindings array grows by 1)."""
    existing_config = {
        "baseHash": "hash1",
        "config": {
            "agents": {},
            "bindings": [
                {"agentId": "existing-agent", "channel": "feishu", "peer": "peer-0"}
            ],
        },
    }
    patch_response = MagicMock()
    patch_response.json.return_value = {"ok": True}
    patch_response.raise_for_status = MagicMock()

    get_response = MagicMock()
    get_response.json.return_value = existing_config
    get_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = [get_response, patch_response]
        await client.add_binding(
            agent_id="new-agent", channel="feishu", peer="peer-1"
        )

    # Second call is the patch — check that bindings array grew by 1
    patch_call = mock_post.call_args_list[1]
    sent_patch = patch_call.kwargs["json"]["patch"]
    assert len(sent_patch["bindings"]) == 2
    assert sent_patch["bindings"][1]["agentId"] == "new-agent"
    assert sent_patch["bindings"][1]["channel"] == "feishu"
    assert sent_patch["bindings"][1]["peer"] == "peer-1"


async def test_remove_binding(client):
    """Verifies GET + filter + PATCH (binding removed from array)."""
    existing_config = {
        "baseHash": "hash2",
        "config": {
            "agents": {"keep-agent": {}, "remove-agent": {}},
            "bindings": [
                {"agentId": "keep-agent", "channel": "feishu", "peer": "peer-0"},
                {"agentId": "remove-agent", "channel": "feishu", "peer": "peer-1"},
            ],
        },
    }
    patch_response = MagicMock()
    patch_response.json.return_value = {"ok": True}
    patch_response.raise_for_status = MagicMock()

    get_response = MagicMock()
    get_response.json.return_value = existing_config
    get_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = [get_response, patch_response]
        await client.remove_binding("remove-agent")

    patch_call = mock_post.call_args_list[1]
    sent_patch = patch_call.kwargs["json"]["patch"]
    assert len(sent_patch["bindings"]) == 1
    assert sent_patch["bindings"][0]["agentId"] == "keep-agent"


async def test_add_agent_with_binding(client):
    """Verifies atomic add of agent definition + binding in one patch."""
    existing_config = {
        "baseHash": "hash3",
        "config": {
            "agents": {"old-agent": {"name": "Old"}},
            "bindings": [
                {"agentId": "old-agent", "channel": "feishu", "peer": "peer-0"}
            ],
        },
    }
    patch_response = MagicMock()
    patch_response.json.return_value = {"ok": True}
    patch_response.raise_for_status = MagicMock()

    get_response = MagicMock()
    get_response.json.return_value = existing_config
    get_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = [get_response, patch_response]
        await client.add_agent_with_binding(
            agent_id="new-agent",
            agent_config={"name": "New Agent", "model": "gpt-4"},
            channel="feishu",
            peer="peer-1",
        )

    patch_call = mock_post.call_args_list[1]
    sent_patch = patch_call.kwargs["json"]["patch"]
    # Agent definition added
    assert "new-agent" in sent_patch["agents"]
    assert sent_patch["agents"]["new-agent"]["name"] == "New Agent"
    # Binding appended (array replaced entirely with old + new)
    assert len(sent_patch["bindings"]) == 2
    assert sent_patch["bindings"][1]["agentId"] == "new-agent"


# ── ConfigPatchQueue ────────────────────────────────────────────────


async def test_queue_flush_merges_operations():
    """Queue merges multiple operations into a single config.patch call."""
    mock_client = AsyncMock(spec=ConfigPatchClient)
    mock_client.get_config.return_value = {
        "baseHash": "hash-q",
        "config": {
            "agents": {},
            "bindings": [],
        },
    }
    mock_client.patch_config.return_value = {"ok": True}

    queue = ConfigPatchQueue(mock_client)
    await queue.enqueue({"add_agent": {"name": "A1"}, "agent_id": "agent-1"})
    await queue.enqueue({"add_binding": {"agentId": "agent-1", "channel": "feishu", "peer": "p1"}})
    await queue.enqueue({"remove_binding_agent_id": "old-agent"})

    await queue.flush_now()

    # get_config called once
    mock_client.get_config.assert_called_once()
    # patch_config called once with merged result
    mock_client.patch_config.assert_called_once()
    call_args = mock_client.patch_config.call_args
    base_hash = call_args.args[0]
    patch = call_args.args[1]
    assert base_hash == "hash-q"
    assert "agent-1" in patch["agents"]
    assert any(b["agentId"] == "agent-1" for b in patch["bindings"])


async def test_queue_retry_on_conflict():
    """Queue retries up to MAX_RETRIES on baseHash conflict."""
    mock_client = AsyncMock(spec=ConfigPatchClient)
    mock_client.get_config.return_value = {
        "baseHash": "hash-retry",
        "config": {"agents": {}, "bindings": []},
    }
    # First two attempts raise conflict, third succeeds
    mock_client.patch_config.side_effect = [
        Exception("baseHash conflict"),
        Exception("baseHash conflict"),
        {"ok": True},
    ]

    queue = ConfigPatchQueue(mock_client)
    await queue.enqueue({"add_agent": {"name": "A1"}, "agent_id": "agent-1"})
    await queue.flush_now()

    assert mock_client.patch_config.call_count == 3
    assert mock_client.get_config.call_count == 3


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

    # Should not raise — drops batch after MAX_RETRIES
    await queue.flush_now()

    assert mock_client.patch_config.call_count == ConfigPatchQueue.MAX_RETRIES
