/**
 * OpenClaw Sidecar Routing Plugin
 *
 * 1. Slash command registry — /help, /status, etc. handled without LLM
 * 2. Fallback routing — provision/restore/deny for unmatched messages
 *
 * Uses the `before_dispatch` hook which fires after routing
 * but before agent invocation.
 */

const SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791";
const DEFAULT_ACCOUNT_ID = "shared";

// ---------------------------------------------------------------------------
// Slash Command Registry
// ---------------------------------------------------------------------------

const _commands = new Map();

/**
 * Register a slash command.
 * @param {string} name - Command name including "/" (e.g. "/help")
 * @param {object} opts
 * @param {string} opts.description - Short description for /help
 * @param {boolean} [opts.adminOnly] - Require admin group context
 * @param {(event, ctx, sidecarUrl) => Promise<string|null>} opts.handler
 *   Return reply text, or null to skip (let agent handle).
 */
function registerCommand(name, { description, adminOnly = false, handler }) {
  _commands.set(name.toLowerCase(), { description, adminOnly, handler });
}

/**
 * Try to match and execute a slash command.
 * Returns {handled, text} or null if no match.
 */
async function trySlashCommand(content, event, ctx, sidecarUrl) {
  const trimmed = (content || "").trim();
  if (!trimmed.startsWith("/")) return null;

  const parts = trimmed.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const entry = _commands.get(cmd);
  if (!entry) return null;

  // Admin-only commands: only work in admin group context
  // (sessionKey contains the admin agent binding)
  if (entry.adminOnly) {
    const sessionKey = ctx.sessionKey || "";
    if (!sessionKey.includes("admin")) {
      return { handled: true, text: `${cmd} 仅限管理员在管理群中使用` };
    }
  }

  try {
    const result = await entry.handler(event, ctx, sidecarUrl);
    if (result === null) return null; // handler chose not to handle
    return { handled: true, text: result };
  } catch (err) {
    return { handled: true, text: `${cmd} 执行失败: ${err.message}` };
  }
}

// ---------------------------------------------------------------------------
// Built-in Commands
// ---------------------------------------------------------------------------

registerCommand("/help", {
  description: "显示所有可用命令",
  handler: async () => {
    const lines = ["📋 OneSyn小龙虾 可用命令：", ""];
    for (const [name, { description, adminOnly }] of _commands) {
      const badge = adminOnly ? " [管理员]" : "";
      lines.push(`• ${name} — ${description}${badge}`);
    }
    return lines.join("\n");
  },
});

registerCommand("/status", {
  description: "查看系统状态（agent 数量统计）",
  adminOnly: true,
  handler: async (_event, _ctx, sidecarUrl) => {
    const resp = await fetch(`${sidecarUrl}/api/v1/agents`);
    if (!resp.ok) return "无法获取 agent 状态";
    const { agents } = await resp.json();
    const active = agents.filter((a) => a.status === "active").length;
    const suspended = agents.filter((a) => a.status === "suspended").length;
    const total = agents.length;
    return `📊 系统状态\n• 总计: ${total} 个 agent\n• 活跃: ${active}\n• 已暂停: ${suspended}`;
  },
});

registerCommand("/agents", {
  description: "列出所有 agent",
  adminOnly: true,
  handler: async (_event, _ctx, sidecarUrl) => {
    const resp = await fetch(`${sidecarUrl}/api/v1/agents`);
    if (!resp.ok) return "无法获取 agent 列表";
    const { agents } = await resp.json();
    if (agents.length === 0) return "当前没有任何 agent";
    const lines = ["📋 Agent 列表：", ""];
    for (const a of agents) {
      const id = a.open_id || a.chat_id || "?";
      lines.push(`• ${a.agent_id} [${a.status}] — ${a.agent_type} ${id}`);
    }
    return lines.join("\n");
  },
});

registerCommand("/logs", {
  description: "查看最近操作日志",
  adminOnly: true,
  handler: async (_event, _ctx, sidecarUrl) => {
    const resp = await fetch(`${sidecarUrl}/api/v1/audit-log?limit=10`);
    if (!resp.ok) return "无法获取操作日志";
    const { logs } = await resp.json();
    if (logs.length === 0) return "暂无操作日志";
    const lines = ["📜 最近操作日志：", ""];
    for (const l of logs) {
      const time = l.timestamp.slice(0, 19).replace("T", " ");
      lines.push(`• [${time}] ${l.action} → ${l.target} (by ${l.actor})`);
    }
    return lines.join("\n");
  },
});

// ---------------------------------------------------------------------------
// Main Plugin
// ---------------------------------------------------------------------------

export default {
  id: "openclaw-sidecar",
  name: "Sidecar Routing",
  description: "Slash commands + fallback routing via Sidecar API",

  register(api) {
    const cfg = api.pluginConfig || {};
    const sidecarUrl = cfg.sidecarUrl || SIDECAR_DEFAULT_URL;
    const accountId = cfg.accountId || DEFAULT_ACCOUNT_ID;

    api.logger.info(
      `Sidecar plugin registered (url=${sidecarUrl}, account=${accountId}, commands=${_commands.size})`
    );

    api.on("before_dispatch", async (event, ctx) => {
      // 1. Only intercept messages from the shared account
      if (ctx.accountId && ctx.accountId !== accountId) return;

      // 2. Try slash commands first (works for ALL agents, not just fallback)
      const content = event.content || "";
      const slashResult = await trySlashCommand(content, event, ctx, sidecarUrl);
      if (slashResult) return slashResult;

      // 3. Only intercept non-slash messages routed to fallback agent
      const sessionKey = ctx.sessionKey || event.sessionKey || "";
      if (!sessionKey.includes("fallback")) return;

      // ── Group messages ───────────────────────────────────────────
      if (event.isGroup) {
        const conversationId = ctx.conversationId || event.conversationId;
        if (!conversationId) {
          api.logger.warn("before_dispatch: group message without conversationId, skipping");
          return;
        }

        api.logger.info(`Intercepting fallback group message in ${conversationId}`);

        try {
          const resolveResp = await fetch(`${sidecarUrl}/api/v1/resolve-sender`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: conversationId }),
          });

          if (!resolveResp.ok) {
            api.logger.error(`resolve-sender (group) failed: ${resolveResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          const { action } = await resolveResp.json();
          api.logger.info(`resolve-sender (group): ${conversationId} → ${action}`);

          if (action === "active") return;

          if (action === "provision_group") {
            const provResp = await fetch(`${sidecarUrl}/api/v1/provision-group`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ chat_id: conversationId }),
            });
            if (provResp.ok) {
              api.logger.info(`Provisioned group agent for ${conversationId}`);
              return { handled: true, text: "群助手正在准备中，请稍后再 @ 我" };
            }
            api.logger.error(`provision-group failed: ${provResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          api.logger.warn(`Unexpected group action: ${action}`);
          return;
        } catch (err) {
          api.logger.error(`Sidecar API error (group): ${err.message}`);
          return;
        }
      }

      // ── DM messages ─────────────────────────────────────────────
      const senderId = ctx.senderId || event.senderId;
      if (!senderId) {
        api.logger.warn("before_dispatch: no senderId, skipping");
        return;
      }

      api.logger.info(`Intercepting fallback message from ${senderId}`);

      try {
        const resolveResp = await fetch(`${sidecarUrl}/api/v1/resolve-sender`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ open_id: senderId }),
        });

        if (!resolveResp.ok) {
          api.logger.error(`resolve-sender failed: ${resolveResp.status}`);
          return { handled: true, text: "系统维护中，请稍后重试" };
        }

        const { action, message } = await resolveResp.json();
        api.logger.info(`resolve-sender: ${senderId} → ${action}`);

        switch (action) {
          case "provision": {
            const provResp = await fetch(`${sidecarUrl}/api/v1/provision`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ open_id: senderId }),
            });
            if (provResp.ok) {
              api.logger.info(`Provisioned agent for ${senderId}`);
              return { handled: true, text: "正在为您准备专属助手，请再发一条消息开始对话" };
            }
            api.logger.error(`provision failed: ${provResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          case "restore": {
            const restResp = await fetch(`${sidecarUrl}/api/v1/restore`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ open_id: senderId }),
            });
            if (restResp.ok) {
              api.logger.info(`Restored agent for ${senderId}`);
              return { handled: true, text: "您的助手已恢复，请再发一条消息继续对话" };
            }
            api.logger.error(`restore failed: ${restResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          case "deny":
            return { handled: true, text: message || "您没有权限使用本助手，如需使用请联系管理员" };

          case "deny_silent":
            return { handled: true };

          case "retry_later":
            return { handled: true, text: "您的助手正在准备中，请稍后再试" };

          default:
            api.logger.warn(`Unknown action: ${action}`);
            return;
        }
      } catch (err) {
        api.logger.error(`Sidecar API error: ${err.message}`);
        return;
      }
    });
  },
};
