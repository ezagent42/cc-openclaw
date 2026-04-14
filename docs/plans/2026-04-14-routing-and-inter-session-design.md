# Design: 精准路由 + 实例间通信 + 完成总结

> Date: 2026-04-14
> Status: Approved

## Changes

### 1. 精准路由（Exact-only dispatch）

**Current:** exact match 时 wildcard 也收到消息（observation 模式）
**New:** exact match 时只发给匹配的 instance，wildcard 不收到

```
飞书消息 → channel_server route_message()
├── exact match found → 只发给该 instance
├── no exact + wildcard exists → 发给 wildcard（保底 session）
└── no exact + no wildcard → admin group member 提示 / 其他静默
```

**Files:** `feishu/channel_server.py` — modify `route_message()`

### 2. 实例间通信（Inter-session forward）

新增消息类型 `forward`，允许 CC instance 向其他 instance 发消息。

**channel.py** — 新增 MCP tool `forward`:
```
forward(target_instance: str, text: str) → 发送消息给指定 instance
```

**channel_server.py** — 新增 `_handle_forward`:
```
从 instance registry 查找 target_instance_id
发送 {type: "forwarded_message", from: sender_instance_id, text: ...}
target instance 的 channel.py 收到后注入为 MCP notification
```

### 3. 完成总结（Summary to monitor）

新增 MCP tool `send_summary`:
```
send_summary(summary: str) → 向 monitor session 发送任务完成总结
```

实现：`send_summary` 是 `forward` 的特化版，固定 target 为 monitor instance。

**CLAUDE.md templates** — 在 superadmin 和 operator 的 CLAUDE.md 中加入指令：
```
当你完成一个任务后，请调用 send_summary 工具向管理群发送一条简短的任务总结。
```

### 4. Monitor 接收总结

monitor session 收到 forwarded_message 时，将其展示在管理群中（通过 reply tool 发到管理群）。

## File Changes

| File | Change |
|------|--------|
| `feishu/channel_server.py` | route_message 精准路由 + _handle_forward |
| `feishu/channel.py` | 新增 forward + send_summary MCP tools + forwarded_message 接收 |
| `roles/superadmin/CLAUDE.md` | 加入 send_summary 指令 |
| `roles/operator/CLAUDE.md` | 加入 send_summary 指令 |
| `roles/monitor/CLAUDE.md` | 加入接收 summary 的行为说明 |

## Message Flow Examples

**Example 1: 精准路由**
```
林懿伦 DM → channel_server → exact match "linyilun" → 只发给 linyilun session
（monitor 不收到，wildcard 不收到）
```

**Example 2: 实例间通信**
```
linyilun session: forward("monitor", "请注意：刚部署了新版本")
→ channel_server 查找 "monitor" instance
→ 发送给 monitor session
→ monitor 在管理群展示
```

**Example 3: 完成总结**
```
linyilun session 完成代码修改
→ Claude 调用 send_summary("完成了 channel_server 路由优化，3 个文件修改")
→ channel_server → monitor session
→ monitor 在管理群展示: "[linyilun] 完成了 channel_server 路由优化，3 个文件修改"
```
