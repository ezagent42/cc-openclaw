# tests/test_token_server.py
"""Tests for Feishu auth verification in the token server."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from voice.token_server import verify_feishu_auth_code, generate_jssdk_signature


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
async def test_verify_valid_auth_code():
    """Two-step flow succeeds: app_access_token + OIDC exchange."""
    app_resp = _mock_response({"code": 0, "app_access_token": "a-xxx"})
    user_resp = _mock_response({
        "code": 0,
        "data": {"access_token": "u-xxx", "open_id": "ou_abc", "name": "Test"},
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_resp, user_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result["open_id"] == "ou_abc"
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_verify_invalid_auth_code():
    """App token OK, OIDC exchange fails → None."""
    app_resp = _mock_response({"code": 0, "app_access_token": "a-xxx"})
    fail_resp = _mock_response({"code": 10012, "msg": "invalid code"})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_resp, fail_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("bad", "app_id", "secret")
    assert result is None


@pytest.mark.asyncio
async def test_verify_app_token_fails():
    """App token request fails → None (fail closed)."""
    fail_resp = _mock_response({"code": 10003, "msg": "bad app"})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=fail_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result is None


@pytest.mark.asyncio
async def test_verify_network_error():
    """Network error → None."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result is None


def test_jssdk_signature():
    """Signature matches expected SHA1 format."""
    sig = generate_jssdk_signature("ticket123", "nonce456", "1700000000", "https://voice.ezagent.chat")
    assert len(sig) == 40  # SHA1 hex
    assert sig == generate_jssdk_signature("ticket123", "nonce456", "1700000000", "https://voice.ezagent.chat")
