# Design: 管理小龙虾多用户 CC Session 架构

> Date: 2026-04-14
> Status: Draft

## Problem

管理小龙虾（OneSyn管理小龙虾）当前是单 CC session，所有消息进同一个 Claude 上下文。需要支持：
- 管理群作为独立的群 agent（监控/总结）
- 多个管理员各自有独立的 DM CC session
- 不同管理员有不同权限（总管理员 vs 运营管理员）
- 总管理员手动控制哪些 CC session 启动

## Architecture

```
管理小龙虾 (cli_a9564804f1789cc9)
       │
  channel_server.py (随机端口, PID 文件)
       │
       ├── 管理群 (oc_e75a27e1...)
       │   └── exact route → CC session "monitor"
       │       role: monitor (只读, 监控/总结/转发)
       │
       ├── 林懿伦 DM (oc_d9b47511...)
       │   └── exact route → CC session "linyilun"
       │       role: superadmin (完整权限)
       │
       ├── 王语欣 DM (oc_xxx...)
       │   └── exact route → CC session "wangyuxin"
       │       role: operator (只读+数据查询)
       │
       └── 未注册用户 DM
           ├── 在管理群中 → 回复"您的管理 session 尚未启动，请联系总管理员"
           └── 不在管理群 → 静默不回复
```

## Directory Structure

```
cc-openclaw/
├── roles/
│   ├── roles.yaml              # 角色定义 + 用户映射
│   ├── superadmin/
│   │   ├── settings.json       # CC 权限配置 (bypassPermissions + hooks)
│   │   └── CLAUDE.md           # 角色注入指令 (完整权限)
│   ├── operator/
│   │   ├── settings.json       # 受限权限
│   │   └── CLAUDE.md           # 角色注入指令 (只读+查询)
│   └── monitor/
│       ├── settings.json       # 极简权限
│       └── CLAUDE.md           # 角色注入指令 (监控/总结)
├── .workspace/                 # gitignore, 用户临时文件
│   ├── linyilun/
│   ├── wangyuxin/
│   └── monitor/
```

## roles.yaml

```yaml
roles:
  superadmin:
    description: "总管理员，完整权限"
    permissions:
      - all

  operator:
    description: "运营管理员，只读+数据查询"
    permissions:
      - read
      - sidecar-query
      - feishu-query
    restrictions:
      - no-git-push
      - no-service-restart
      - no-file-write-outside-workspace

  monitor:
    description: "管理群监控 agent"
    permissions:
      - read
    restrictions:
      - no-exec
      - no-file-write

users:
  linyilun:
    role: superadmin
    open_id: "ou_6b11faf8e93aedfb9d3857b9cc23b9e7"
    display_name: "林懿伦"

  # 添加更多用户:
  # wangyuxin:
  #   role: operator
  #   open_id: "ou_xxx"
  #   display_name: "王语欣"
```

## cc-openclaw.sh Changes

### Help 信息

```
Usage: cc-openclaw.sh [OPTIONS]

Options:
  --user <name>       启动指定用户的 CC session (从 roles.yaml 查找角色)
  --group             启动管理群 monitor session
  --list              列出所有已配置的用户和角色
  --status            查看当前运行中的 CC session
  --stop <name>       停止指定用户的 CC session
  --help              显示此帮助信息

Examples:
  cc-openclaw.sh                    # 交互式选择模式 (现有行为)
  cc-openclaw.sh --user linyilun    # 启动林懿伦的 superadmin session
  cc-openclaw.sh --user wangyuxin   # 启动王语欣的 operator session
  cc-openclaw.sh --group            # 启动管理群 monitor session
  cc-openclaw.sh --list             # 列出所有用户
  cc-openclaw.sh --status           # 查看运行中的 session
```

### 启动流程 (--user mode)

```bash
cc-openclaw.sh --user linyilun
```

1. 读取 `roles/roles.yaml`，查找 `linyilun` 的角色 (`superadmin`)
2. 确保 tmux session `cc-openclaw` 存在（不存在则创建）
3. 创建 `.workspace/linyilun/` 目录
4. 在 tmux session 中创建新 window `linyilun`
5. 设置环境变量：
   - `OPENCLAW_CHAT_ID=oc_d9b47511...` (从 channel_server 历史或手动配置获取)
   - `OPENCLAW_RUNTIME_MODE=discussion`
   - `OPENCLAW_USER=linyilun`
   - `OPENCLAW_ROLE=superadmin`
6. 启动 claude：
   ```bash
   tmux new-window -t cc-openclaw -n linyilun \
     "claude --permission-mode bypassPermissions \
       --dangerously-load-development-channels server:openclaw-channel \
       --mcp-config .mcp.json \
       --settings roles/superadmin/settings.json" \; \
     run-shell "sleep 3" \; \
     send-keys Enter
   ```
7. channel.py 读取 `OPENCLAW_CHAT_ID` 注册 exact route

### 启动流程 (--group mode)

```bash
cc-openclaw.sh --group
```

类似 `--user`，但：
- 用户名固定为 `monitor`
- chat_id 为管理群 `oc_e75a27e1...`
- 使用 `roles/monitor/settings.json`
- tmux window 名为 `monitor`

### tmux session 管理

```bash
# 启动时确保 session 存在
tmux has-session -t cc-openclaw 2>/dev/null || tmux new-session -d -s cc-openclaw

# 每个用户一个 window
tmux new-window -t cc-openclaw -n linyilun "claude ..." \; run-shell "sleep 3" \; send-keys Enter

# --status 查看
tmux list-windows -t cc-openclaw

# --stop 停止
tmux kill-window -t cc-openclaw:linyilun
```

## channel_server Changes

### 未路由 DM 处理

当前行为：`WARNING No route for chat_id=xxx, message dropped`

改为：
1. 检查 sender 是否在管理群成员列表中
2. 是 → 回复"您的管理 session 尚未启动，请联系总管理员开启"
3. 否 → 静默不回复

实现：channel_server 启动时从飞书 API 拉取管理群成员列表（复用 sidecar 的 reconciler 逻辑或独立调用），缓存在内存中。

### chat_id 映射

用户的 DM chat_id 在首次 DM 时才知道。处理方式：

1. 用户首次 DM 管理小龙虾 → channel_server 记录 `open_id → chat_id` 映射到本地文件
2. `cc-openclaw.sh --user xxx` 启动时从映射文件读取 chat_id
3. channel.py 注册该 chat_id 的 exact route

映射文件：`.workspace/chat_id_map.json`
```json
{
  "ou_6b11faf8e93aedfb9d3857b9cc23b9e7": "oc_d9b47511b085e9d5b66c4595b3ef9bb9",
  "ou_xxx": "oc_yyy"
}
```

## Role Settings

### superadmin/settings.json
```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true,
  "permissions": {
    "allow": ["*"]
  }
}
```

### operator/settings.json
```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true,
  "permissions": {
    "allow": ["Read", "Glob", "Grep", "Bash"],
    "deny": ["Write", "Edit"]
  }
}
```

### monitor/settings.json
```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true,
  "permissions": {
    "allow": ["Read", "Glob", "Grep"],
    "deny": ["Write", "Edit", "Bash"]
  }
}
```

## What Changes

| Component | Change |
|-----------|--------|
| `cc-openclaw.sh` | 新增 `--user`, `--group`, `--list`, `--status`, `--stop`, `--help` |
| `roles/` | 新目录：roles.yaml + 每角色 settings.json + CLAUDE.md |
| `.workspace/` | 新目录：用户临时文件 (gitignore) |
| `channel_server.py` | 未路由 DM 提示 + chat_id 映射记录 |
| `channel.py` | 读取 `OPENCLAW_CHAT_ID` env 注册 exact route |
| `.gitignore` | 添加 `.workspace/` |

## What Doesn't Change

- Sidecar / Gateway / OpenClaw plugin（OneSyn小龙虾 的事）
- 现有 channel_server 消息路由逻辑（exact + wildcard）
- 飞书 App 配置
