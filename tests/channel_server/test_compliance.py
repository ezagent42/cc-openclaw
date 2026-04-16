"""Architecture compliance tests — prevent actor model regression."""
from __future__ import annotations

import inspect
from pathlib import Path
from glob import glob


def test_adapter_files_have_no_metadata_writes():
    """Adapter source must not write to actor.metadata."""
    for path in glob("channel_server/adapters/**/*.py", recursive=True):
        source = Path(path).read_text()
        for i, line in enumerate(source.split("\n"), 1):
            if "actor.metadata" in line and "=" in line and "# compliance-exempt" not in line:
                if ".get(" not in line and "isinstance" not in line:
                    raise AssertionError(f"{path}:{i} writes to actor.metadata: {line.strip()}")


def test_handler_files_have_no_api_imports():
    """Handler source must not import lark_oapi or call feishu_client."""
    for path in glob("channel_server/core/handlers/*.py"):
        source = Path(path).read_text()
        assert "lark_oapi" not in source, f"{path} imports lark_oapi"
        assert "feishu_client" not in source, f"{path} references feishu_client"


def test_transport_handlers_are_async():
    """All transport handler methods must be async."""
    from channel_server.adapters.feishu.adapter import FeishuAdapter
    from channel_server.core.runtime import ActorRuntime
    from unittest.mock import MagicMock

    rt = ActorRuntime()
    adapter = FeishuAdapter(rt, MagicMock())
    assert inspect.iscoroutinefunction(adapter._handle_chat_transport)
    assert inspect.iscoroutinefunction(adapter._handle_thread_transport)


def test_no_threading_in_adapters():
    """No threading.Thread in adapter files (except WS listener)."""
    for path in glob("channel_server/adapters/**/*.py", recursive=True):
        source = Path(path).read_text()
        for i, line in enumerate(source.split("\n"), 1):
            if "threading.Thread" in line and "# compliance-exempt" not in line:
                raise AssertionError(f"{path}:{i} uses threading.Thread: {line.strip()}")


def test_adapter_has_no_business_state():
    """FeishuAdapter must not hold _last_msg_id, _ack_reactions, _seen, _recent_sent."""
    from channel_server.adapters.feishu.adapter import FeishuAdapter
    from channel_server.core.runtime import ActorRuntime
    from unittest.mock import MagicMock

    adapter = FeishuAdapter(ActorRuntime(), MagicMock())
    for attr in ["_last_msg_id", "_ack_reactions", "_seen", "_recent_sent", "_chat_id_map"]:
        assert not hasattr(adapter, attr), f"FeishuAdapter still has {attr}"
