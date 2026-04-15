# Unified Message Type System

## Problem

Actor model 分支中 `Message.type`、`payload["type"]`、`payload["command"]` 三个字段语义混淆，导致：

1. 入站消息被静默丢弃（`payload` 缺少 `type: "message"`，channel.py 过滤掉）
2. 出站回复无法发送（`payload.type = "reply"` 不被 feishu transport handler 识别）
3. 飞书特有操作（react、send_file）污染了通用协议层

## Design

### Core Principle

纯 actor 模型：消息发给谁，谁就决定怎么处理。Message 本身不携带路由语义——路由由地址决定，行为由 handler 决定。

### Message 定义

```python
class Delivery(str, Enum):
    """传输语义 — 消息怎么传递。

    ONESHOT — 一次性发送完整内容
    STREAM  — 建立流式连接，内容分块传输（如语音）
    """
    ONESHOT = "oneshot"
    STREAM = "stream"


@dataclass
class Message:
    sender: str
    delivery: Delivery = Delivery.ONESHOT
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
```

去掉 `Message.type`。原先由 `type` 承载的信息分流到：

- **路由**：由目标地址决定（发给 `feishu:*` = 出站，发给 `cc:*` = 转发，发给 `system:*` = 系统）
- **操作**：由 `payload["action"]` 声明（`"react"`、`"send_file"` 等），仅 transport handler 关心
- **内容格式**：由 `payload["msg_type"]` 声明（`"text"`、`"image"` 等），仅 parsers 关心

### Payload 约定

Payload 是一个 flat dict，刻意不做嵌套分层。当前系统规模下（少量消息类型、单团队代码库），flat dict 比结构化嵌套更直接。如果未来增加更多 transport 类型，可以考虑分为 `payload.routing` / `payload.content` / `payload.action`。

字段按关切分为三类（共存于同一个 dict 中）：

| 关切 | 字段 | 说明 |
|------|------|------|
| 内容 | `text`, `file_path`, `emoji_type`, `title` | 消息/操作的具体内容 |
| 寻址 | `chat_id`, `message_id`, `msg_id`, `card_msg_id`, `target`, `parent_feishu` | 路由和目标标识 |
| 判别 | `action`, `msg_type` | 决定怎么处理这条消息 |

#### 入站消息（飞书 → actor 系统）

由 `FeishuAdapter.on_feishu_event()` 构建。注意 `msg_type` 和 `file_path` 都必须放入 payload（当前代码缺失这两个字段）：

```python
payload = {
    "msg_type": "text",           # 飞书消息格式，决定 parser 选择
    "text": "能收到吗？",          # parser 输出的文本
    "file_path": "",              # parser 输出的文件路径（如有）
    "chat_id": "oc_xxx",
    "message_id": "om_xxx",
}
```

`msg_type` 值域由 `parsers.py` 的 `@register_parser` 注册器管理，目前支持：`text`、`post`、`image`、`file`、`audio`、`media`、`interactive`、`merge_forward`、`sticker`、`share_chat`、`share_user`、`location`、`todo`、`system`、`hongbao`、`vote`、`video_chat`、`calendar`、`folder`。

`file_path` 是服务器本地的绝对路径（由 parsers 下载到 `PROJECT_ROOT/.openclaw/uploads/` 目录）。单机部署下 CC session 可直接 Read 该路径。

#### 出站操作（actor → 外部）

CC session 发出的操作通过 `payload["action"]` 区分：

| action | 含义 | 附加字段 |
|--------|------|----------|
| (无/缺省) | 发送文本消息 | `text` |
| `"react"` | 添加表情回应 | `message_id`, `emoji_type` |
| `"send_file"` | 发送文件 | `chat_id`, `file_path` |
| `"update_title"` | 更新话题卡片标题 | `msg_id`, `title` |
| `"tool_notify"` | 更新工具活动卡片 | `text` |
| `"send_summary"` | 向 root 主聊天发送进度 | `text` |

#### WebSocket 协议（CC adapter ↔ channel.py）

WebSocket JSON 将 `"type"` 重命名为 `"method"`，表示协议操作。所有方向的所有消息（包括注册握手 `_register` 的请求和响应）都使用 `"method"` 而非 `"type"`。

**Client → Server:**

| method | 用途 |
|--------|------|
| `"register"` | 注册 CC session |
| `"reply"` | 回复飞书消息 |
| `"react"` | 添加表情 |
| `"send_file"` | 发送文件 |
| `"forward"` | 转发到另一个 session |
| `"send_summary"` | 发送进度通知 |
| `"update_title"` | 更新话题标题 |
| `"tool_notify"` | 工具活动通知 |
| `"spawn_session"` | 创建子 session |
| `"kill_session"` | 结束子 session |
| `"list_sessions"` | 列出活跃 sessions |
| `"pong"` | 心跳响应 |

**Server → Client:**

| method | 用途 |
|--------|------|
| `"message"` | 飞书入站消息推送 |
| `"registered"` | 注册确认 |
| `"forwarded_message"` | 跨 session 转发消息 |
| `"spawn_result"` / `"kill_result"` / `"sessions_list"` | 管理响应 |
| `"ping"` | 心跳 |
| `"error"` | 错误 |

CC adapter 负责将 WebSocket `method` 翻译为 actor Message：

```python
# CC 发来: {"method": "react", "message_id": "om_xxx", "emoji_type": "THUMBSUP"}
# CC adapter 翻译为:
Message(
    sender="cc:linyilun.root",
    payload={"action": "react", "message_id": "om_xxx", "emoji_type": "THUMBSUP"},
)
# 发送到 downstream feishu actor
```

```python
# CC 发来: {"method": "reply", "text": "收到！", "chat_id": "oc_xxx"}
# CC adapter 翻译为:
Message(
    sender="cc:linyilun.root",
    payload={"text": "收到！", "chat_id": "oc_xxx"},
)
# action 缺省 = 发送文本消息
```

### Handler 改动

#### FeishuInboundHandler

```python
def handle(self, actor, msg):
    if msg.sender.startswith("feishu_user:"):
        # 入站 → 转发给 downstream
        return [Send(to=addr, message=msg) for addr in actor.downstream]
    
    # 出站 → 根据 payload["action"] 调用对应 Feishu API
    return [TransportSend(payload=msg.payload)]
```

不变，但去掉了对 `msg.type` 的任何依赖。

#### CCSessionHandler

```python
def handle(self, actor, msg):
    if msg.sender != actor.address:
        # 外部消息 → 推送到 CC (WebSocket)
        return [TransportSend(payload={
            "method": "message",  # WebSocket 协议 method
            **msg.payload,
        })]
    
    # CC 自身发出的操作
    action = msg.payload.get("action")
    
    if action is None:
        # 默认：回复文本，发给 downstream
        text = msg.payload.get("text", "")
        if actor.tag != "root":
            text = f"[{actor.tag}] {text}"
        reply_msg = Message(sender=actor.address, payload={**msg.payload, "text": text})
        return [Send(to=addr, message=reply_msg) for addr in actor.downstream]
    
    if action == "forward":
        target = msg.payload.get("target", "")
        return [Send(to=target, message=msg)]
    
    if action == "send_summary":
        parent_feishu = msg.payload.get("parent_feishu", "")
        return [Send(to=parent_feishu, message=msg)]
    
    if action == "tool_notify":
        # 路由到 tool_card actor，不是 downstream feishu actor
        user_session = actor.address.replace("cc:", "")  # e.g. "linyilun.dev"
        tool_card_addr = f"tool_card:{user_session}"
        return [Send(to=tool_card_addr, message=msg)]
    
    # react, send_file, update_title → 发给 downstream feishu actor
    return [Send(to=addr, message=msg) for addr in actor.downstream]
```

关键变化：
- 不再用 `payload["command"]`，统一用 `payload["action"]`
- `tool_notify` 路由到 `tool_card:*` actor 而非 downstream（tool_card actor 有自己的 transport）

#### AdminHandler

```python
def handle(self, actor, msg):
    text = msg.payload.get("text", "").strip()
    
    # 系统通知 → 转发 downstream
    # 依赖 C1 修复：msg_type 现在在 payload 中
    if msg.payload.get("msg_type") == "system":
        return [Send(to=addr, message=msg) for addr in actor.downstream]
    
    # ... 其余逻辑不变
```

用 `payload["msg_type"]` 替代原先的 `msg.type == "system"`。

#### ToolCardHandler

```python
class ToolCardHandler:
    def handle(self, actor, msg):
        text = msg.payload.get("text", "")
        history = list(actor.metadata.get("history", []))
        history.append(text)
        if len(history) > 5:
            history = history[-5:]
        display = "\n".join(history)
        return [
            UpdateActor(changes={"metadata": {"history": history}}),
            TransportSend(payload={"action": "tool_notify", "text": display}),
        ]
```

输出 payload 统一用 `action: "tool_notify"`（原先是 `type: "tool_card_update"`）。

#### Feishu Transport Handlers

```python
def _handle_chat_transport(self, actor, payload):
    action = payload.get("action")
    chat_id = actor.transport.config["chat_id"]
    
    if action is None:
        # 默认：发送文本消息
        text = payload.get("text", "")
        self._send_message(chat_id, text, None)
    elif action == "react":
        self._send_reaction(payload["message_id"], payload.get("emoji_type", "THUMBSUP"))
    elif action == "send_file":
        self._send_file(payload.get("chat_id", chat_id), payload["file_path"])
    elif action == "tool_notify":
        msg_id = payload.get("card_msg_id", "")
        self._update_card(msg_id, payload.get("text", ""))
    else:
        log.warning("Unhandled action=%s on %s", action, actor.address)

def _handle_thread_transport(self, actor, payload):
    action = payload.get("action")
    config = actor.transport.config
    chat_id = config.get("chat_id", "")
    root_id = config.get("root_id", "")
    
    if action is None:
        text = payload.get("text", "")
        self._send_message(chat_id, text, root_id)
    elif action == "react":
        self._send_reaction(payload["message_id"], payload.get("emoji_type", "THUMBSUP"))
    elif action == "send_file":
        self._send_file(payload.get("chat_id", chat_id), payload["file_path"])
    elif action == "update_title":
        self._update_anchor_card(payload.get("msg_id", ""), payload.get("title", ""))
    elif action == "tool_notify":
        msg_id = payload.get("card_msg_id", "")
        self._update_card(msg_id, payload.get("text", ""))
    else:
        log.warning("Unhandled action=%s on %s", action, actor.address)
```

用 `payload["action"]` 替代原先的 `payload["type"]`。统一用 `"tool_notify"` 而非 `"tool_card_update"`。

### CC Adapter 改动

`handle_message` dispatch 从 `msg.get("type")` 改为 `msg.get("method")`。

`_handle_register` 的响应从 `{"type": "registered"}` 改为 `{"method": "registered"}`。

`_route_to_actor` 将 WebSocket `method` 翻译为 `payload["action"]`：

```python
def _route_to_actor(self, ws, msg):
    method = msg.get("method", "")
    payload = dict(msg)
    payload.pop("method", None)
    
    # reply → 无 action（默认发文本）
    # 其余 method → 设为 action
    if method != "reply":
        payload["action"] = method
    
    # send_summary 需要注入 parent_feishu 路由信息
    if method == "send_summary":
        actor = self.runtime.lookup(address)
        if actor and actor.parent:
            parent = self.runtime.lookup(actor.parent)
            if parent:
                parent_feishu = next(
                    (d for d in parent.downstream if d.startswith("feishu:")), ""
                )
                if parent_feishu:
                    payload["parent_feishu"] = parent_feishu
    
    actor_msg = Message(sender=address, payload=payload)
    self.runtime.send(address, actor_msg)
```

### channel.py 改动

`_message_loop` 从 `msg.get("type")` 改为 `msg.get("method")`：

```python
async def _message_loop(self, ws):
    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "message":
            await self._message_queue.put(msg)
        elif method == "forwarded_message":
            ...
        elif method in ("spawn_result", "kill_result", "sessions_list"):
            ...
        elif method == "ping":
            await ws.send(json.dumps({"method": "pong"}))
        elif method == "error":
            log.error("Server error: %s", msg)
        else:
            log.warning("Unhandled method=%r keys=%s", method, list(msg.keys()))
```

`_register` 从检查 `resp.get("type")` 改为 `resp.get("method")`。

所有 send 方法从 `"type"` 改为 `"method"`：

```python
async def send_reply(self, chat_id, text):
    await self.ws.send(json.dumps({"method": "reply", "chat_id": chat_id, "text": text}))

async def send_summary(self, text):
    # 字段名统一为 "text"（原先用 "summary"）
    await self.ws.send(json.dumps({"method": "send_summary", "text": text}))
```

### 日志格式

Runtime `_actor_loop` 的日志从展示 `msg.type` 改为展示 `payload.get("action")` 或 `payload.get("msg_type")`：

```python
action = msg.payload.get("action") or msg.payload.get("msg_type", "message")
log.info("Actor %s processing msg from %s action=%s", actor.address, msg.sender, action)
```

### 不变的部分

- `parsers.py` 和 `@register_parser` 注册器完全不动
- `Delivery` enum 加入但目前只有 `ONESHOT`，voice 支持时加 `STREAM`
- `Actor`、`Transport`、`Action` 类型不变
- `TransportSend` 不变——它只是把 payload 交给 transport handler

### 文件改动清单

| 文件 | 改动 |
|------|------|
| `core/actor.py` | `Message` 去掉 `type`，加 `delivery: Delivery`，加 `Delivery` enum |
| `core/handler.py` | 所有 handler 去掉 `msg.type` 引用，改用 `payload["action"]` / `payload["msg_type"]`；`ToolCardHandler` 输出改用 `action: "tool_notify"` |
| `core/runtime.py` | 日志格式改用 `action` |
| `adapters/cc/adapter.py` | `handle_message` dispatch 用 `method`；`_handle_register` 响应用 `method`；`_route_to_actor` 翻译 method → action 并保留 `send_summary` 的 `parent_feishu` 注入 |
| `adapters/cc/channel.py` | `_message_loop` 和 `_register` 用 `method`；所有 send 方法用 `method`；`send_summary` 字段名改 `text` |
| `adapters/feishu/adapter.py` | `on_feishu_event` 构建 Message 不设 type，payload 加入 `msg_type` 和 `file_path`；transport handler 用 `action` dispatch，统一用 `tool_notify` |
| `adapters/feishu/parsers.py` | 不变 |
| `tests/` | 更新所有 Message 构造和断言 |
