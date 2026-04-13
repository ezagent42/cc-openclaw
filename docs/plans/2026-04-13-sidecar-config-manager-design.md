# Design: 内部小龙虾 Sidecar 配置管理服务

> Date: 2026-04-13  
> Status: Approved  
> PRD: `docs/prd/prd-feishu-openclaw-bridge.md` v0.4  
> Approach: 方案 B — 配置管理者 + fallback agent 轻量 hook

---

## 1. Architecture Summary

Sidecar 是一个独立的配置管理服务，**不拦截消息流**。OpenClaw Gateway 通过 openclaw-lark 插件直连飞书（WebSocket 模式），使用 peer-based binding 路由消息到各 agent。Sidecar 监听飞书群成员变化事件，通过 `config.patch` RPC 动态管理 agent 定义和 peer binding。

```
飞书 App ←WebSocket→ OpenClaw Gateway (openclaw-lark 原生)
                         ↕ peer binding 路由
                  user agents / group agents / admin / fallback

Sidecar (独立进程):
  ├─ 飞书 WebSocket 事件监听 (群成员变化)
  ├─ SQLite: 权限表 + 注册表 + 审计日志 + 频控表
  ├─ config.patch RPC (限速队列, 5s 合并窗口)
  ├─ 管理 API (127.0.0.1:18791)
  └─ 定期对账器 (10 分钟)

耦合点:
  Sidecar → OpenClaw: config.patch RPC
  OpenClaw → Sidecar: fallback/admin agent 调管理 API
```

### 为什么不拦截消息流

- 复用 openclaw-lark 的飞书消息处理（card 渲染、流式输出、文件上传、UAT 文档隔离）
- 已有用户对话不依赖 Sidecar 运行，Sidecar 崩溃影响最小化
- 后续对话延迟与原生 OpenClaw 一致

### 已验证的关键假设

- OpenClaw peer-based routing 支持 `match: { channel: "feishu", peer: { kind: "direct", id: "ou_xxx" } }`
- openclaw-lark 使用 UAT (per-user token)，文档访问天然隔离
- `config.patch` RPC 可用，限速 3次/60秒，bindings 数组整体替换
- hot reload (hybrid 模式) 对 agent/binding 变更即时生效

---

## 2. SQLite Schema

```sql
CREATE TABLE permission (
    open_id        TEXT PRIMARY KEY,
    display_name   TEXT,
    is_user_member BOOLEAN DEFAULT FALSE,
    is_admin       BOOLEAN DEFAULT FALSE,
    updated_at     TEXT NOT NULL  -- ISO8601
);

CREATE TABLE agent_registry (
    agent_id       TEXT PRIMARY KEY,  -- u-{accountId}-{open_id} 或 g-{accountId}-{chat_id}
    open_id        TEXT,              -- DM agent 的用户 (群 agent 为 NULL)
    chat_id        TEXT,              -- 群 agent 的 chat_id (DM agent 为 NULL)
    agent_type     TEXT NOT NULL,     -- 'user' | 'group'
    status         TEXT NOT NULL DEFAULT 'active',  -- active | suspended | archived
    workspace_path TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    suspended_at   TEXT,
    restored_at    TEXT
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    action      TEXT NOT NULL,  -- provision|suspend|restore|archive|reset|
                                -- permission_granted|permission_revoked|reconciled_*
    target      TEXT NOT NULL,
    actor       TEXT NOT NULL,  -- 'system' | 'reconciler' | admin open_id
    details     TEXT            -- JSON
);

CREATE TABLE deny_rate_limit (
    open_id         TEXT PRIMARY KEY,
    last_denied_at  TEXT NOT NULL,
    deny_count      INTEGER DEFAULT 1
);
```

---

## 3. Module Structure

```
sidecar/
├── __init__.py
├── main.py                 # 入口: 启动事件监听 + 管理 API
├── db.py                   # SQLite 连接 + schema migration + CRUD
├── feishu_events.py        # 飞书 WebSocket 事件监听器
│                           #   群成员加入/移除 (事件名称待验证)
│                           #   bot 入群 / 群解散
├── permission.py           # 权限表管理 + 对账逻辑
├── provisioner.py          # Agent 生命周期: provision / suspend / restore / archive / reset
│                           #   模板复制 + USER.md 渲染
│                           #   config.patch RPC 调用
├── config_patch.py         # config.patch RPC 封装
│                           #   baseHash 获取 (config.get)
│                           #   限速队列 (3次/60秒, 5秒窗口合并)
│                           #   bindings 数组 GET+append+PATCH
├── api.py                  # 管理 API (HTTP server, 127.0.0.1:18791)
│                           #   GET  /api/v1/check-permission
│                           #   GET  /api/v1/agents
│                           #   GET  /api/v1/audit-log
│                           #   POST /api/v1/provision
│                           #   POST /api/v1/restore
│                           #   POST /api/v1/deny-check
│                           #   POST /api/v1/admin/reset-agent
├── reconciler.py           # 定期对账 (10 分钟间隔)
└── templates/
    ├── user-agent/
    │   ├── SOUL.md
    │   ├── USER.md.tmpl    # {{display_name}} 等占位符
    │   ├── IDENTITY.md
    │   └── ...
    └── group-agent/
        ├── SOUL.md
        └── ...
```

---

## 4. config.patch Strategy

### 限速与合并

```python
class ConfigPatchQueue:
    RATE_LIMIT = 3          # 次/60秒
    MERGE_WINDOW = 5        # 秒: 窗口内变更合并

    async def enqueue(self, patch: dict):
        # 深度合并到 pending_patch
        # 5 秒定时器到期时 _flush()

    async def _flush(self):
        # 1. GET config.get → baseHash + 当前 config
        # 2. 合并: 当前 bindings + pending 新增/移除
        # 3. POST config.patch (baseHash + merged)
        # 4. 轮询 config.get 确认生效
        # 5. baseHash 冲突 → 重新获取后重试
```

### bindings 数组处理

`config.patch` 的 JSON merge patch 语义下，数组是整体替换。每次修改 bindings:
1. `config.get` 获取当前完整 bindings 数组
2. 在内存中 append/remove
3. 提交整个数组

合并窗口减少并发冲突概率。

### Provision 产生的 patch

```json5
{
  "agents": {
    "u-cli_xxx-ou_alice": {
      "agentDir": "~/.openclaw/agents/u-cli_xxx-ou_alice/agent",
      "workspace": "~/.openclaw/agents/u-cli_xxx-ou_alice/workspace",
      "model": "openrouter/google/gemini-3.1-flash-lite-preview"
    }
  },
  "bindings": [
    // ... 现有 bindings ...
    {
      "agentId": "u-cli_xxx-ou_alice",
      "match": { "channel": "feishu", "peer": { "kind": "direct", "id": "ou_alice" } }
    }
  ]
}
```

---

## 5. Key Flows

### 首次 DM → 懒创建

```
用户 DM → OpenClaw (无 peer binding) → fallback agent
  → GET /api/v1/check-permission?open_id=ou_xxx
  → { authorized: true, agent_exists: false }
  → POST /api/v1/provision?open_id=ou_xxx
  → Sidecar: 复制模板 + 渲染 USER.md + config.patch
  → fallback 回复: "正在准备，请再发一条消息"
  → 用户第二条消息 → peer binding 匹配 → 专属 agent
```

### 用户离群 → suspend

```
飞书群成员移除事件 → Sidecar feishu_events.py
  → permission.py: UPDATE is_user_member=FALSE
  → provisioner.py: config.patch 移除 peer binding
  → agent_registry: status=suspended
  → audit_log: suspend
```

### 重新入群 → restore

```
飞书群成员加入事件 → Sidecar: 权限表恢复
用户 DM → fallback → GET check-permission → authorized + suspended
  → POST /api/v1/restore → config.patch 恢复 peer binding
  → fallback 回复: "助手已恢复"
  → 用户第二条消息 → agent (数据无损)
```

### 未授权 → 拒绝 + 频控

```
用户 DM → fallback → GET /api/v1/deny-check?open_id=ou_xxx
  → { should_reply: true/false, message: "..." }
  → fallback 决定回复或静默
```

---

## 6. Fallback Agent Design

**配置**: 静态配置在 openclaw.json，binding 优先级最低（无 peer 条件 = catch-all）

**行为**: 确定性逻辑，不调用 LLM。通过 MCP tools 或 shell tools 调 Sidecar API:

```
收到消息 →
  1. 调 check-permission(sender open_id)
  2. if authorized + no agent → 调 provision → 回复"准备中"
  3. if authorized + suspended → 调 restore → 回复"已恢复"
  4. if not authorized → 调 deny-check → 根据频控决定回复或静默
  5. if Sidecar API 不可达 → 回复"系统维护中，请稍后重试"
```

**SOUL.md** 需要明确指示 agent 不调用 LLM，仅执行上述确定性逻辑。

---

## 7. Admin Agent Design

**配置**: 静态配置，peer binding 匹配管理群 `{ kind: "group", id: "oc_admin_xxx" }`

**行为**: 调用 LLM 理解自然语言管理指令，通过 tools 调 Sidecar 管理 API:

- `list-agents`: `GET /api/v1/agents?status=xxx`
- `audit-log`: `GET /api/v1/audit-log?since=xxx`
- `reset-agent`: `POST /api/v1/admin/reset-agent` (需二次确认)

---

## 8. Reconciler

每 10 分钟:
1. 飞书 API 全量拉取用户群 + 管理群成员
2. 与 SQLite permission 表 diff
3. 修正不一致 (补授权/撤权限)
4. 与 agent_registry 交叉检查 binding 状态
5. audit_log 记录所有修正 (标记为 reconciled_*)

启动时也执行一次全量对账。

对账**不主动 provision/restore agent**，只修正权限表和 binding。Agent 创建/恢复仍然是懒触发。

---

## 9. Migration Plan (14 apps → 1 shared app)

### Step 1: 并行运行 (零风险)
- 保留 14 个飞书 App + accountId binding
- 新增共享 App account 配置
- 启动 Sidecar，导入现有用户 open_id
- 为现有用户添加 peer binding（与旧 binding 并存）
- 验证共享 App 路由

### Step 2: 逐用户切换
- 确认共享 App 路由正常 → 停用旧 App → 移除旧 binding
- 一次一个，有问题回退

### Step 3: 清理
- 移除所有旧 App account 配置
- 最终只剩共享 App + peer bindings + fallback + admin

---

## 10. Deployment

```
launchd services:
  ai.openclaw.gateway    (已有) → openclaw gateway --port 18789
  ai.openclaw.sidecar    (新增) → uv run python3 sidecar/main.py
                                   KeepAlive: true
                                   stdout → ~/.openclaw/logs/sidecar.log
```

Sidecar 管理 API: `127.0.0.1:18791` (loopback only)
SQLite: `~/.openclaw/sidecar.sqlite`

### Sidecar Config

```yaml
# sidecar-config.yaml
feishu:
  app_id: "cli_xxx"
  app_secret: "${FEISHU_APP_SECRET}"
  user_group_chat_id: "oc_user_xxx"
  admin_group_chat_id: "oc_admin_xxx"

openclaw:
  gateway_url: "http://127.0.0.1:18789"
  auth_token: "${OPENCLAW_AUTH_TOKEN}"
  default_model: "openrouter/google/gemini-3.1-flash-lite-preview"
  account_id: "shared"

sidecar:
  api_port: 18791
  db_path: "~/.openclaw/sidecar.sqlite"
  reconcile_interval_minutes: 10
  deny_rate_limit_minutes: 10

templates:
  user_agent_dir: "./templates/user-agent/"
  group_agent_dir: "./templates/group-agent/"
```

---

## 11. PRD Differences Summary

| PRD v0.3 | v0.4 Design | Reason |
|----------|-------------|--------|
| Sidecar 拦截消息做权限检查 | 只管配置，不碰消息流 | 复用 openclaw-lark 原生连接 |
| HTTP webhook 模式 | 保持 WebSocket | 飞书 SDK 支持，无需公网入口 |
| Sidecar 直接调飞书 API 拒绝 | fallback agent + Sidecar API | 不拦截消息流 |
| 未授权零 LLM 消耗 | 经过 fallback 但不调 LLM | 资源消耗可忽略 |
| 首次 5 秒单条直达 | fallback 提示 + 用户重发 | 懒创建 + peer routing 的 trade-off |
| 多飞书 App + accountId | 单 App + peer routing | 目标架构方向 |
