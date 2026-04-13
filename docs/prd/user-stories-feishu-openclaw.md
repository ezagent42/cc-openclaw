# User Stories: 内部小龙虾 (飞书 × OpenClaw 桥接)

> Companion document to `prd-feishu-openclaw-bridge.md` v0.4  
> Format: Mike Cohn user story + Gherkin acceptance criteria  
> Personas: Bob (管理员) · Alice (用户) · Charlie (群参与者)
>
> **v0.3 更新重点**: 权限模型统一为"用户群驱动"，增加 suspend/restore 相关 stories，增加未授权频控和强制重置 stories
> **v0.4 更新重点**: 架构调整为"Sidecar 配置管理者 + OpenClaw peer routing + fallback agent"模式。主要变更：(1) Sidecar 不再拦截消息流，改为通过 config.patch 管理 peer binding；(2) 未授权拒绝由 fallback agent 调 Sidecar API 实现；(3) 首次对话体验变为两步（首条消息落到 fallback → provision → 用户重发）；(4) 飞书事件名称标注为"待验证"；(5) 从多飞书 App 迁移为单 App + peer routing

---

## 视角一: 管理员 (Bob)

> **Persona:** Bob 是被拉进管理群的运维人员。熟悉 OpenClaw、Linux、Python。他的核心目标是让系统在零手动维护下运行，权限管理只通过"拉人进群 / 踢人出群"完成。

---

### User Story B-001

- **Summary:** 部署时配置用户群和管理群 chat_id，让系统知道授权来源

#### Use Case:
- **As a** 内部小龙虾系统的部署者
- **I want to** 在 Sidecar 配置文件中写入"用户群"和"管理群"的 chat_id
- **so that** 系统知道哪些飞书群的成员关系是权限的来源，而不需要把权限逻辑散落在各处

#### Acceptance Criteria:

- **Scenario:** 部署时配置授权群
- **Given:** Sidecar 尚未启动，但已经存在一个空的配置文件 `sidecar-config.yaml`
- **and Given:** 我已经在飞书里创建了一个"用户群"(chat_id = `oc_user_xxx`) 和一个"管理群" (chat_id = `oc_admin_xxx`)
- **and Given:** 共享飞书 App 已经被加进这两个群
- **and Given:** OpenClaw Gateway 已配置共享飞书 App 的 account、fallback agent 和 admin agent
- **When:** 我在 Sidecar 配置中写入 `user_group_chat_id: oc_user_xxx` 和 `admin_group_chat_id: oc_admin_xxx`，然后启动 Sidecar
- **Then:** Sidecar 启动时调用飞书 API 一次性拉取这两个群的当前成员，写入 SQLite 权限表；启动独立 WebSocket 连接订阅这两个群的成员变化事件（注：事件名称待验证）；启动管理 API server (127.0.0.1:18791)；启动 10 分钟定期对账

---

### User Story B-002

- **Summary:** 把人拉进用户群即授予使用权，不需要改配置文件

#### Use Case:
- **As a** 管理员
- **I want to** 只通过把人拉进用户群这一个动作来授予他使用 bot 的权限
- **so that** 我不需要记住任何 CLI 命令或改任何配置文件，权限管理完全在飞书的 UI 里完成

#### Acceptance Criteria:

- **Scenario:** 把新用户拉进用户群
- **Given:** Sidecar 正在运行，已通过独立 WebSocket 订阅用户群的成员变化事件
- **and Given:** 权限表中不存在 `ou_alice`
- **and Given:** Alice 当前 DM bot 会被 fallback agent 拒绝
- **When:** 我在飞书里把 Alice 拉进用户群
- **Then:** Sidecar 在 10 秒内收到群成员加入事件（事件名称待验证）并把 `ou_alice` 加入权限表；audit_log 中插入一条 `permission_granted` 记录；此时 Alice 首次 DM bot 时，fallback agent 会为她触发 provision，第二条消息开始正常路由到专属 agent

---

### User Story B-003

- **Summary:** 把人踢出用户群即收回权限，对应 DM agent 进入 suspended 状态

#### Use Case:
- **As a** 管理员
- **I want to** 把某人踢出用户群即收回他的 bot 使用权，同时让他的 DM agent 进入 suspended 状态 (数据保留但路由移除)
- **so that** 权限收回是即时且可逆的，如果后续发现是误踢还可以原样恢复

#### Acceptance Criteria:

- **Scenario:** 从用户群踢出已有 DM agent 的用户
- **Given:** Alice 之前已经在用户群里，且她的 DM agent `u-cli_xxx-ou_alice` 已经处于 active 状态，有对应的 peer binding
- **and Given:** Alice 的 agent workspace 目录 `~/.openclaw/agents/u-cli_xxx-ou_alice/` 存在且包含历史对话数据
- **When:** 我把 Alice 从用户群踢出
- **Then:** Sidecar 在 10 秒内收到群成员移除事件（事件名称待验证），更新权限表移除 `ou_alice`；调 `config.patch` 移除 `u-cli_xxx-ou_alice` 的 peer binding；注册表中该 agent 的 status 更新为 `suspended`；**workspace 目录物理保留在原位不移动**；audit_log 记录 suspend 操作；此后 Alice DM bot 会被 fallback agent 拒绝

---

### User Story B-004

- **Summary:** 被踢用户重新入群后原 agent 无损恢复

#### Use Case:
- **As a** 管理员
- **I want to** 在我重新把某人拉回用户群后，他原有的 DM agent 能够无损恢复，保留所有历史对话和记忆
- **so that** "离群-重入"对用户来说是完全透明的，不会因为权限临时收回而导致数据丢失

#### Acceptance Criteria:

- **Scenario:** 被 suspended 的用户重新入群并再次对话
- **Given:** Alice 之前被踢出用户群，她的 DM agent 处于 suspended 状态（peer binding 已移除）
- **and Given:** Alice 的 workspace 目录完整保留在 `~/.openclaw/agents/u-cli_xxx-ou_alice/`
- **and Given:** workspace 中的 SOUL.md / USER.md / sessions/ 都还在
- **When:** 我把 Alice 重新拉进用户群，Sidecar 更新权限表恢复授权
- **and When:** Alice DM bot 一条新消息
- **Then:** 消息无 peer binding 匹配 → 路由到 fallback agent；fallback 调 Sidecar API 发现 authorized + suspended → 触发 restore 路径：只调 `config.patch` 恢复 peer binding，不复制模板、不渲染 USER.md、不触碰 workspace 任何文件；注册表状态更新为 active；fallback 回复"您的助手已恢复，请再发一条消息继续对话"；Alice 的第二条消息被 peer binding 路由到 agent，agent 加载原有 session 后能够识别出这是一个已知用户并引用过往对话内容

---

### User Story B-005

- **Summary:** 把人拉进管理群即授予管理员权限

#### Use Case:
- **As a** 初始管理员
- **I want to** 把另一个运维同事拉进管理群即让他获得管理员权限
- **so that** 我可以灵活地添加或移除管理员，而不需要在配置文件里硬编码 open_id 列表

#### Acceptance Criteria:

- **Scenario:** 新增管理员
- **Given:** 我是当前的管理员之一，已在管理群里
- **and Given:** Carol 是另一个运维同事，当前不在管理群，权限表中没有她的 admin 标记
- **When:** 我把 Carol 拉进管理群
- **Then:** Sidecar 在 10 秒内更新权限表，给 `ou_carol` 加上 admin 标记；Carol 在管理群 @ bot 时被视为管理员；Carol 直接 DM bot 时也会自动拥有使用权 (即使她不在用户群里)；audit_log 记录 `admin_granted` 操作

---

### User Story B-006

- **Summary:** 在管理群 @ bot 查询所有 agent 状态

#### Use Case:
- **As a** 管理员
- **I want to** 在管理群 @ 内部小龙虾询问系统当前的 agent 列表和状态
- **so that** 我能随时掌握系统的整体健康状况而不需要 SSH 登录服务器

#### Acceptance Criteria:

- **Scenario:** 管理员在管理群查询 agent 列表
- **Given:** 我在管理群里
- **and Given:** 管理群对应的 admin agent 已经通过静态 binding 预配置完毕
- **and Given:** 系统当前有若干个 user agent 和 group agent，状态各异 (active / suspended)
- **When:** 我在管理群发送 "@内部小龙虾 列出所有 agent"
- **Then:** admin agent 调用 Sidecar 的管理 API `GET /api/v1/agents`，返回结构化数据；admin agent 在管理群回复一条格式化消息，按 status 分组列出所有 agent，包括 agent_id、display_name、status、最近活跃时间

---

### User Story B-007

- **Summary:** 强制重置某用户的 agent，清空历史对话和记忆

#### Use Case:
- **As a** 管理员
- **I want to** 在必要时 (比如用户投诉记忆污染、合规要求重置) 强制重置某用户的 agent
- **so that** 我能处理异常情况，清除被污染或不合规的 agent 数据，让该用户下次对话时从全新状态开始

#### Acceptance Criteria:

- **Scenario:** 管理员强制重置用户 agent
- **Given:** Alice 的 DM agent 当前处于 active 状态，workspace 中有历史数据
- **and Given:** 我在管理群里
- **When:** 我在管理群发送 "@内部小龙虾 重置 Alice 的 agent"
- **and When:** admin agent 回复二次确认问 "确认要重置 Alice 的 agent 吗？这将清空她所有历史对话和记忆"
- **and When:** 我回复"确认"
- **Then:** admin agent 调用 `POST /api/v1/admin/reset-agent`；Sidecar 调 `config.patch` 移除 agent + binding，mv workspace 到 `~/.openclaw/archived/u-cli_xxx-ou_alice-reset-{timestamp}/`，注册表删除该条目；audit_log 记录 `forced_reset` 操作；Alice 下次 DM bot 时触发全新 provision，收到首次见面欢迎语而不是 restore 欢迎语

---

### User Story B-008

- **Summary:** 未授权用户的消息不消耗任何 LLM 资源

#### Use Case:
- **As a** 关心系统成本的管理员
- **I want to** 确保未授权用户发来的消息被 Sidecar 直接拒绝，不进入 OpenClaw 不消耗任何 token
- **so that** 恶意或误发的消息不会产生 LLM 费用，也不会污染 OpenClaw 的 sessions 数据

#### Acceptance Criteria:

- **Scenario:** 未授权用户发送消息
- **Given:** Dan 当前不在用户群也不在管理群，权限表中没有他的记录
- **When:** Dan 向 bot 发送一条消息
- **Then:** OpenClaw 无 `ou_dan` 的 peer binding → 路由到 fallback agent；fallback 调 Sidecar API `deny-check`，确认未授权 → 回复"您没有权限使用本助手，如需使用请联系管理员"；整个流程 **不调用 LLM**（fallback 是确定性逻辑，仅调 Sidecar API + 发送固定文案），LLM token 消耗为零
- **注意 (v0.4 变更):** 消息经过了 fallback agent（与 v0.3 "消息不进入 OpenClaw" 不同），但 fallback 不调用 LLM，资源消耗可忽略

---

### User Story B-009

- **Summary:** 未授权用户的重复消息被频控，避免被骚扰

#### Use Case:
- **As a** 管理员
- **I want to** 让未授权用户即使反复发消息，系统也最多每 10 分钟回复一次拒绝消息
- **so that** 恶意刷消息的攻击者不会变成"用系统回复骚扰其他人"的放大器，同时正常用户的误发也不会收到重复通知

#### Acceptance Criteria:

- **Scenario:** 未授权用户短时间内多次发消息
- **Given:** Dan 不在任何授权群里
- **and Given:** deny_rate_limit 表中没有 `ou_dan` 的记录 (第一次被拒)
- **When:** Dan 在 1 分钟内向 bot 连续发送 5 条消息
- **Then:** 所有 5 条消息都路由到 fallback agent；fallback 每次调 Sidecar API `deny-check`；只有第一条消息 Sidecar 返回 `should_reply: true`，fallback 回复"没有权限"；后 4 条消息 Sidecar 返回 `should_reply: false`（10 分钟频控窗口内），fallback 静默不回复；deny_rate_limit 表中记录 `ou_dan` 的最近一次拒绝时间
- **and When:** Dan 在第一次被拒后 11 分钟再次发消息 (超过 10 分钟窗口)
- **Then:** Sidecar deny-check 返回 `should_reply: true`，fallback 再次回复"没有权限"，更新 deny_rate_limit 记录

---

### User Story B-010

- **Summary:** 查询审计日志追溯所有权限和 agent 操作

#### Use Case:
- **As a** 管理员
- **I want to** 查看系统过去 N 天内的所有关键操作 (provision / suspend / restore / archive / reset / 权限变更)
- **so that** 当出现问题时我能追溯发生了什么，并能向合规部门提供操作记录

#### Acceptance Criteria:

- **Scenario:** 查询近 7 天审计日志
- **Given:** Sidecar 的 SQLite 中存在 audit_log 表
- **and Given:** 过去 7 天内系统发生了多次 provision / suspend / restore 等操作
- **and Given:** 我在管理群里
- **When:** 我在管理群发送 "@内部小龙虾 查看最近 7 天的操作日志"
- **Then:** admin agent 调用 `GET /api/v1/audit-log?since=2026-04-06`，返回按时间倒序排列的操作记录；每条记录包含 timestamp、action (provision/suspend/restore/archive/reset/permission_granted/permission_revoked)、target (agent_id 或 open_id)、actor (system 或 admin open_id)、details；admin agent 格式化后在群里回复

---

### User Story B-011

- **Summary:** 权限表与飞书群成员状态定期对账，避免事件丢失

#### Use Case:
- **As a** 管理员
- **I want to** 让 Sidecar 定期全量拉取用户群和管理群的成员列表，跟权限表做 diff 并自动修正
- **so that** 即使某次飞书事件推送失败，系统也能通过对账自愈，不会长期陷入"权限表过期"的状态

#### Acceptance Criteria:

- **Scenario:** 飞书成员变化事件丢失后的自动对账
- **Given:** Sidecar 配置中设置 `reconcile_interval_minutes: 10`
- **and Given:** Alice 已经被从用户群踢出，但那次群成员移除事件因网络问题没送达 Sidecar
- **and Given:** 权限表中 Alice 仍然被标记为 authorized (过期状态)，peer binding 仍然存在
- **When:** Sidecar 的对账循环触发 (10 分钟间隔)，调用飞书 API 全量拉取用户群成员列表
- **Then:** Sidecar 发现实际成员中没有 Alice，但权限表中有 → 触发修正：移除 Alice 的权限 + 调 `config.patch` 移除 peer binding (suspend) + 写 audit_log 标注为 `reconciled_suspend`

---

### User Story B-012

- **Summary:** Sidecar 崩溃后重启能自动恢复所有状态

#### Use Case:
- **As a** 管理员
- **I want to** 即使 Sidecar 临时崩溃或重启，期间和之后的系统行为都是一致的
- **so that** 单点故障不会永久损坏系统状态，我不需要在故障后手动清理数据

#### Acceptance Criteria:

- **Scenario:** Sidecar 崩溃并被 launchd 重启
- **Given:** Sidecar 正在运行，处理群成员变化事件
- **and Given:** 已实现基于 event_id 的幂等去重 (持久化在 SQLite)
- **and Given:** launchd 配置了 `KeepAlive: true`
- **When:** Sidecar 崩溃，launchd 在数秒内将其重启
- **Then:** 启动时 Sidecar 重新加载 SQLite 中的权限表和注册表；立即执行一次全量对账修正崩溃期间可能丢失的事件；重新建立飞书 WebSocket 事件监听；重新启动管理 API server
- **关键 (v0.4):** Sidecar 崩溃期间 **已有用户的对话完全不受影响**，因为 peer binding 已持久化在 openclaw.json 中，OpenClaw Gateway 独立处理消息。受影响的仅是：新用户无法 provision、权限变更暂停、fallback agent 无法调通 Sidecar API（会返回连接错误，fallback 应有降级处理）

---

## 视角二: 用户 (Alice)

> **Persona:** Alice 是任意一个被管理员拉进用户群的人 (可能是内部员工，也可能是外部合作伙伴或客户)。她不知道也不需要知道 OpenClaw 是什么。她只想在飞书里直接得到一个专属的、隔离的、记住她的 AI 助手。

---

### User Story A-001

- **Summary:** 第一次对话时收到友好欢迎，确认这是专属助手

#### Use Case:
- **As a** 被刚拉进用户群的新用户
- **I want to** 在首次 DM bot 时收到欢迎消息，让我知道这是我的专属 AI 助手而不是共享 bot
- **so that** 我能立即理解它的定位，建立信任，愿意继续对话

#### Acceptance Criteria:

- **Scenario:** 用户首次 DM bot
- **Given:** 我从未跟内部小龙虾对话过
- **and Given:** 管理员已经把我拉进用户群，我的 open_id 已在 Sidecar 权限表中
- **and Given:** user-agent 模板的 SOUL.md 包含"首次见面"逻辑
- **When:** 我向 bot 发送第一条消息 (例如 "你好")
- **Then:** 消息路由到 fallback agent（因为我还没有 peer binding）；fallback 调 Sidecar API 确认我已授权但无 agent → 触发 provision；fallback 回复我"正在为您准备专属助手，请再发一条消息开始对话"
- **and When:** 我发送第二条消息
- **Then:** peer binding 已生效，消息路由到我的专属 agent；agent 回复中包含我的名字 (从 USER.md 读取)、说明这是我的专属 AI 助手、说明对话内容完全私密、邀请我开始使用
- **注意 (v0.4 变更):** 首次对话需要两条消息才能到达专属 agent（v0.3 设计为 5 秒内单条消息直达）。这是复用 OpenClaw 原生 peer routing 的 trade-off

---

### User Story A-002

- **Summary:** 已授权用户的常规对话延迟与普通 IM bot 一致

#### Use Case:
- **As a** 已经用过 bot 的用户
- **I want to** 后续每次发消息都能在 1-2 秒内开始收到回复
- **so that** 我能像使用普通 IM bot 一样流畅地跟它对话

#### Acceptance Criteria:

- **Scenario:** 已注册用户的后续对话
- **Given:** 我的 DM agent 已经处于 active 状态，peer binding 已在 openclaw.json 中
- **and Given:** OpenClaw Gateway 处于正常运行状态
- **When:** 我向 bot 发送一条普通消息
- **Then:** OpenClaw peer routing 直接匹配我的 peer binding，路由到我的 agent；**Sidecar 不参与此流程**；首字节响应时间与原生 OpenClaw 一致，不超过 1.5 秒 (P95)
- **注意 (v0.4 变更):** 后续对话完全不经过 Sidecar，延迟与原生 OpenClaw 持平。即使 Sidecar 崩溃，已有用户的对话也不受影响

---

### User Story A-003

- **Summary:** 我的 agent 记得我是谁、记得我们之前聊过什么

#### Use Case:
- **As a** 频繁使用 bot 的用户
- **I want to** 我的 agent 在多次对话之间保留对我的认知 (角色、偏好、正在做的项目) 和对话历史
- **so that** 我不需要每次重复"我是 Alice，是 XX 的 PM，正在做 YY"这样的自我介绍

#### Acceptance Criteria:

- **Scenario:** 跨天的连续对话
- **Given:** 我的 DM agent 已存在
- **and Given:** 第 1 天我跟它说过 "我是 Alice，正在做 hackforger 项目"
- **and Given:** OpenClaw 的 session 持久化正常工作
- **and Given:** 第 1 天对话 session 没有被 reset
- **When:** 第 2 天我发消息 "上次说的那个项目进展如何"
- **Then:** agent 的回复中能正确识别 "那个项目" 指的是 hackforger，表现出对我身份和上下文的记忆，无需重新介绍

---

### User Story A-004

- **Summary:** 我的对话内容只有我自己能看到，不会被其他用户串话

#### Use Case:
- **As a** 重视个人隐私的用户
- **I want to** 确信我跟 bot 的对话内容 (上下文记忆、长期 memory、workspace 文件) 完全隔离于其他用户
- **so that** 我可以放心讨论敏感话题，不担心被其他用户或他们的 agent 看到

#### Acceptance Criteria:

- **Scenario:** 多用户并发使用 + 隔离验证
- **Given:** Alice 和 Bob 都已经有了各自的 DM agent
- **and Given:** `session.dmScope` 设为 `per-channel-peer`
- **and Given:** 每个 agent 有独立 workspace 和 sessions 目录
- **and Given:** Alice 跟自己的 agent 说过 "我下个月要离职创业"
- **When:** Bob 跟自己的 agent 问 "你知道 Alice 最近有什么想法吗"
- **Then:** Bob 的 agent 完全不知道 Alice 提到过离职的事，回复中没有任何关于 Alice 的私人信息泄露；这个隔离可以通过 `openclaw security audit` 自动验证

---

### User Story A-005

- **Summary:** 我被误踢出群后重新入群，我的 agent 完全无损恢复

#### Use Case:
- **As a** 曾经被误踢出用户群又被重新拉回的用户
- **I want to** 我的 agent 的所有历史对话和记忆完全保留，就像我从未离开过一样
- **so that** 管理员的权限变动不会让我丢失任何工作成果

#### Acceptance Criteria:

- **Scenario:** 被踢后重新入群的无损恢复
- **Given:** 一周前我在用户群里，跟我的 agent 有了很多对话，包括让 agent 记住了我的项目计划
- **and Given:** 3 天前我被误踢出用户群，期间尝试 DM bot 被 fallback 提示"没有权限"
- **and Given:** 刚才管理员把我重新拉回用户群
- **When:** 我 DM bot 任意一条消息
- **Then:** 消息落到 fallback → fallback 触发 restore → 回复"您的助手已恢复，请再发一条消息继续对话"
- **and When:** 我发第二条消息 "你还记得我上周让你记的项目计划吗"
- **Then:** peer binding 已恢复，消息路由到我的 agent；agent 的回复准确引用了一周前我告诉它的项目计划内容，证明 session 数据和 workspace 完全无损

---

### User Story A-006

- **Summary:** 未授权用户发消息收到清晰的拒绝说明

#### Use Case:
- **As a** 一个好奇或误发消息的未授权用户
- **I want to** 在我没权限使用 bot 时收到一条清晰友好的说明，而不是石沉大海或看到技术错误
- **so that** 我知道这不是 bug，而是权限问题，并能联系管理员申请权限

#### Acceptance Criteria:

- **Scenario:** 未授权用户的首次尝试
- **Given:** 我当前不在用户群也不在管理群
- **and Given:** deny_rate_limit 表中没有我的记录
- **When:** 我向 bot 发送任意一条消息
- **Then:** 消息路由到 fallback agent；fallback 调 Sidecar API deny-check 确认未授权；fallback 回复"您没有权限使用本助手，如需使用请联系管理员"；**整个流程不调用 LLM**（fallback 是确定性逻辑）

---

### User Story A-007

- **Summary:** 我可以用简单命令重置对话，从全新 session 开始

#### Use Case:
- **As a** 偶尔想"清空脑子重新开始"的用户
- **I want to** 通过 `/reset` 或 `/new` 命令让我的 agent 开启全新对话 session
- **so that** 当对话变得混乱或我想换话题时能快速重置

#### Acceptance Criteria:

- **Scenario:** 用户主动重置对话
- **Given:** 我跟 agent 已经有若干轮对话历史
- **and Given:** OpenClaw 默认支持 `/new` 和 `/reset` 命令
- **When:** 我向 agent 发送独立一条消息 `/new`
- **Then:** OpenClaw 创建新的 session id (旧 JSONL 保留在磁盘但不加载)；agent 回复简短的 "已开始新对话" 确认；后续消息使用新 session 的空白上下文；**注意这是 session reset，不是 agent reset，我的 USER.md 和长期 memory 保留**

---

### User Story A-008

- **Summary:** 在多设备上使用同一个 agent

#### Use Case:
- **As a** 跨设备工作的用户
- **I want to** 在飞书手机版、PC 版、iPad 版上跟同一个 agent 对话，对话历史和状态完全同步
- **so that** 我可以无缝在设备之间切换

#### Acceptance Criteria:

- **Scenario:** 跨设备对话续接
- **Given:** 我的 agent 已经存在
- **and Given:** 我在手机端和 PC 端都用同一个企业账号登录飞书
- **and Given:** OpenClaw 基于 open_id 路由，与设备 ID 无关
- **When:** 我在手机端跟 agent 说 "记一下：明天 10 点开 PRD review 会"，然后切到 PC 端发 "刚才让你记的那件事是什么"
- **Then:** PC 端收到的回复正确包含 "明天 10 点 PRD review 会" 这个信息

---

## 视角三: 群参与者 (Charlie)

> **Persona:** Charlie 是飞书群里的成员。他**必须本身已经在用户群里**才能触发 bot 响应。他在项目群、讨论群里 @ 内部小龙虾作为群助手。

---

### User Story C-001

- **Summary:** 把 bot 拉进群后系统自动为该群创建专属群 agent

#### Use Case:
- **As a** 想把 AI 助手加入我们项目群的用户
- **I want to** 在我把 bot 拉进群的那一刻，系统就为这个群准备好专属的群 agent
- **so that** 群里第一次 @ bot 时不会出现"创建中请稍候"的延迟

#### Acceptance Criteria:

- **Scenario:** Bot 被首次拉进新群
- **Given:** 我本身在用户群里，有使用权
- **and Given:** 一个飞书群 (chat_id = `oc_xxx`) 从未有 bot 参与
- **and Given:** Sidecar 已订阅 bot 入群事件（事件名称待验证）
- **When:** 我在群设置中将 bot 添加为群成员
- **Then:** Sidecar 在 5 秒内收到 bot 入群事件并完成群 agent 的 provision；调 `config.patch` 在 `openclaw.json` 中追加 `g-cli_xxx-oc_xxx` agent 定义和 `peer: { kind: "group", id: "oc_xxx" }` 的 binding；OpenClaw 热重载完成；注册表中插入对应记录

---

### User Story C-002

- **Summary:** 群里 @ bot 时必须本身是授权用户才会被响应

#### Use Case:
- **As a** 群里的普通成员
- **I want to** 只有本身在用户群里的人 @ bot 时才会被响应，未授权的群成员 @ 会被拒绝
- **so that** 外部人员或未授权用户即使混进群也无法滥用 bot 资源

#### Acceptance Criteria:

- **Scenario:** 群里混合有授权和未授权成员
- **Given:** 群 `oc_xxx` 中有 Alice (授权)、Bob (授权)、Eve (未授权) 三人
- **and Given:** 群 agent 已 provision 完毕，有对应的 peer binding `{ kind: "group", id: "oc_xxx" }`
- **and Given:** `channels.feishu.requireMention: true`
- **When:** Eve 在群里发送 "@内部小龙虾 帮我写个周报"
- **Then:** 消息路由到群 agent（群 peer binding 匹配的是 chat_id，不区分 sender）；群 agent 的权限检查逻辑取决于 SOUL.md 配置 —— MVP 阶段群 agent 对所有群成员响应（含未授权成员），后续可在 SOUL.md 中加入 sender 权限检查
- **and When:** 随后 Alice 发送 "@内部小龙虾 总结一下讨论"
- **Then:** 群 agent 正常处理并回复
- **注意 (v0.4 变更):** 由于 Sidecar 不拦截消息流，群内的 per-sender 权限检查无法在路由层实现。MVP 阶段接受群 agent 对所有群成员响应。如需限制，可在群 agent 的 SOUL.md 或 tools 中实现

---

### User Story C-003

- **Summary:** 群里 @ bot 时只在被明确 @ 时响应，平时静默

#### Use Case:
- **As a** 在繁忙群里讨论问题的授权成员
- **I want to** bot 只在明确 @ 它时响应，平时完全静默
- **so that** 群讨论不会被 bot 刷屏

#### Acceptance Criteria:

- **Scenario:** 群里的 mention-only 触发
- **Given:** bot 已在群 `oc_xxx` 中，群 agent 已 provision
- **and Given:** `channels.feishu.requireMention: true`
- **and Given:** 群 agent 的 `groupChat.mentionPatterns` 配置匹配 "@内部小龙虾"
- **When:** 群里有 10 条普通讨论消息 (不 @ bot)，第 11 条是 "@内部小龙虾 帮我们总结上面的讨论"
- **Then:** 前 10 条 bot 完全不响应；第 11 条触发群 agent 处理并回复总结

---

### User Story C-004

- **Summary:** 群 agent 记得整个群的历史讨论

#### Use Case:
- **As a** 希望 bot 能"参与"群讨论的授权成员
- **I want to** 群 agent 能看到群里之前的讨论，即使有些消息没有直接 @ 它
- **so that** 当我 @ bot 时它能基于完整群上下文回答

#### Acceptance Criteria:

- **Scenario:** 群 agent 利用群历史 context
- **Given:** 群 `oc_xxx` 中有若干历史消息，前 5 条是关于 "hackforger 项目设计选择" 的讨论
- **and Given:** OpenClaw 群消息有独立 session key (`agent:g-xxx:feishu:group:oc_xxx`)
- **and Given:** `channels.feishu.historyLimit` 允许群 agent 看到最近 N 条消息作为上下文
- **When:** 我在群里发 "@内部小龙虾 我们刚才的设计讨论里有哪些待决问题"
- **Then:** 群 agent 的回复能准确引用前 5 条讨论的内容，识别出其中的待决问题

---

### User Story C-005

- **Summary:** 群解散时对应的群 agent 自动归档

#### Use Case:
- **As a** 完成项目准备解散群的群主
- **I want to** 在解散群之后，对应的群 agent 自动归档 (停止响应但保留数据)
- **so that** 我不需要手动通知管理员清理，也不会导致已解散群的 agent 长期残留

#### Acceptance Criteria:

- **Scenario:** 群被解散后的自动归档
- **Given:** 群 `oc_xxx` 已有对应的群 agent `g-cli_xxx-oc_xxx`，处于 active 状态
- **and Given:** Sidecar 已订阅 `im.chat.disbanded_v1` 事件
- **When:** 群主解散群 `oc_xxx`，飞书推送 disbanded 事件给 Sidecar
- **Then:** Sidecar 在 60 秒内调 `config.patch` 移除 agent + binding；mv `~/.openclaw/agents/g-cli_xxx-oc_xxx/` 到 `~/.openclaw/archived/g-cli_xxx-oc_xxx-{timestamp}/`；注册表状态更新为 archived；audit_log 记录归档操作

---

## Story Map 一览

```
Phase 1: PoC 必须 (Week 1-2, 验证核心假设)
├─ B-001: 部署时配置用户群和管理群
├─ B-002: 拉人进群即授权 (快速路径)
├─ B-008: 未授权消息不消耗 LLM 资源
├─ A-001: 首次对话欢迎
├─ A-002: 后续对话低延迟
├─ A-004: 用户之间隔离 (P0 安全)
└─ A-006: 未授权用户收到清晰拒绝

Phase 2: MVP 必须 (Week 3-4, 让系统能跑起来)
├─ B-003: 踢出群 → suspended
├─ B-004: 重新入群 → 无损恢复
├─ B-005: 管理员授权
├─ B-006: 管理群查询 agent 列表
├─ B-009: 未授权消息频控
├─ B-010: 审计日志查询
├─ B-011: 定期对账自愈
├─ B-012: Sidecar 崩溃恢复
├─ A-003: 跨对话记忆
├─ A-005: 被踢重入无损恢复 (用户视角)
├─ A-007: /reset 命令
├─ A-008: 多设备同步
├─ C-001: 群 bot 入群自动 provision
├─ C-002: 群里权限检查
├─ C-003: 群里 @ 才响应
└─ C-005: 群解散自动归档

Phase 3: 加固 (Week 5-6, 让运维放心)
├─ B-007: 强制重置
└─ C-004: 群历史上下文
```

---

## Splitting / Refinement Notes

**权限模型统一后的简化收益**：v0.2 中关于"离职员工自动归档"的若干 stories 已全部删除，v0.3 把它们折叠进了"群成员关系同步"这一条主线。

**关键 P0 stories**：
- **A-004 (用户隔离)**：必须做自动化压测，不能只靠手测。写一个测试脚本：N 个用户并发对话 + 跨用户提问 + 检查回复中是否泄露其他用户内容
- **B-008 (未授权资源零消耗)**：必须通过查 OpenClaw 日志和 sessions.json 的完全缺失来验证
- **B-011 (定期对账自愈)**：是"事件驱动 + 定期修正"双保险设计的核心，不做这一条整个权限系统就不可靠

**可能需要拆分的 stories**：
- **B-006 (管理群查询 agent)**：实际包含"Sidecar 管理 API 设计"、"admin agent tool 定义"、"admin agent 消息格式化"三个子任务，可能需要拆
- **A-003 (跨对话记忆)**：依赖 OpenClaw memory 子系统 (QMD) 配置，可能要拆成"短期 session 记忆"和"长期 memory 持久化"两个 story
- **C-004 (群历史)**：取决于 OpenClaw 群消息的 historyLimit 配置上限，可能需要降级

**跨视角 stories (未单独写出但在这里记录)**：
- **系统重启恢复**：B-012 覆盖了 Sidecar 崩溃，但没有 story 覆盖 OpenClaw 本身崩溃。需要一个 B-013: "OpenClaw Gateway 崩溃后重启能加载动态注入的所有 agent"
- **多飞书 app 同名用户**：Alice 在 App A 和 App B 都有 agent，能不能保证完全独立？需要一个 B-014 或 A-009 覆盖这个场景
