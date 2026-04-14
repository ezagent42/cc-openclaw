/**
 * OpenClaw Sidecar Routing Plugin
 *
 * 1. Registered commands — /status, /agents, /logs, /broadcast
 *    Uses api.registerCommand() so they appear in OpenClaw's native /help
 *    and are processed BEFORE built-in commands and agent invocation.
 *
 * 2. Fallback routing — provision/restore/deny for unmatched messages
 *    Uses before_dispatch hook for messages routed to fallback agent.
 */

const SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791";
const DEFAULT_ACCOUNT_ID = "shared";

export default {
  id: "openclaw-sidecar",
  name: "Sidecar Routing",
  description: "Sidecar management commands + fallback routing",

  register(api) {
    const cfg = api.pluginConfig || {};
    const sidecarUrl = cfg.sidecarUrl || SIDECAR_DEFAULT_URL;
    const accountId = cfg.accountId || DEFAULT_ACCOUNT_ID;

    // ------------------------------------------------------------------
    // Registered Commands (appear in /help, processed before LLM)
    // ------------------------------------------------------------------

    api.registerCommand({
      name: "status",
      description: "查看系统状态（agent 数量统计）",
      requireAuth: true,
      handler: async (ctx) => {
        try {
          const resp = await fetch(`${sidecarUrl}/api/v1/agents`);
          if (!resp.ok) return { text: "无法获取 agent 状态" };
          const { agents } = await resp.json();
          const active = agents.filter((a) => a.status === "active").length;
          const suspended = agents.filter((a) => a.status === "suspended").length;
          const total = agents.length;
          return { text: `📊 系统状态\n• 总计: ${total} 个 agent\n• 活跃: ${active}\n• 已暂停: ${suspended}` };
        } catch (e) {
          return { text: `❌ Sidecar 不可达: ${e.message}` };
        }
      },
    });

    api.registerCommand({
      name: "agents",
      description: "列出所有 agent",
      requireAuth: true,
      handler: async (ctx) => {
        try {
          const resp = await fetch(`${sidecarUrl}/api/v1/agents`);
          if (!resp.ok) return { text: "无法获取 agent 列表" };
          const { agents } = await resp.json();
          if (agents.length === 0) return { text: "当前没有任何 agent" };
          const lines = ["📋 Agent 列表：", ""];
          for (const a of agents) {
            const id = a.open_id || a.chat_id || "?";
            lines.push(`• ${a.agent_id} [${a.status}] — ${a.agent_type} ${id}`);
          }
          return { text: lines.join("\n") };
        } catch (e) {
          return { text: `❌ Sidecar 不可达: ${e.message}` };
        }
      },
    });

    api.registerCommand({
      name: "logs",
      description: "查看最近操作日志",
      requireAuth: true,
      handler: async (ctx) => {
        try {
          const resp = await fetch(`${sidecarUrl}/api/v1/audit-log?limit=10`);
          if (!resp.ok) return { text: "无法获取操作日志" };
          const { logs } = await resp.json();
          if (logs.length === 0) return { text: "暂无操作日志" };
          const lines = ["📜 最近操作日志：", ""];
          for (const l of logs) {
            const time = l.timestamp.slice(0, 19).replace("T", " ");
            lines.push(`• [${time}] ${l.action} → ${l.target} (by ${l.actor})`);
          }
          return { text: lines.join("\n") };
        } catch (e) {
          return { text: `❌ Sidecar 不可达: ${e.message}` };
        }
      },
    });

    api.registerCommand({
      name: "broadcast",
      description: "向所有活跃用户群发 DM（用法: /broadcast 消息内容）",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        // ctx has senderId, channel, isAuthorizedSender, etc.
        // The message text after /broadcast is not in ctx directly.
        // We need to get it from the raw content — but registerCommand
        // strips the command prefix. Check if ctx has args or rawContent.
        // Fallback: if no args mechanism, return usage hint.
        const message = ctx.args || ctx.rawContent || "";
        if (!message.trim()) {
          return { text: "用法: /broadcast 消息内容\n例: /broadcast 明天下午3点有产品演示会" };
        }

        try {
          const actor = ctx.senderId || "";
          const resp = await fetch(`${sidecarUrl}/api/v1/admin/broadcast`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: message.trim(), actor }),
          });

          if (resp.status === 403) return { text: "❌ 权限不足，仅管理员可执行群发" };
          if (resp.status === 503) return { text: "❌ 广播服务未配置" };
          if (!resp.ok) return { text: `❌ 群发失败 (${resp.status})` };

          const { sent, failed } = await resp.json();
          const lines = [`✅ 群发完成: 成功 ${sent.length} 人`];
          if (failed.length > 0) lines.push(`⚠️ 失败 ${failed.length} 人`);
          return { text: lines.join("\n") };
        } catch (e) {
          return { text: `❌ Sidecar 不可达: ${e.message}` };
        }
      },
    });

    api.logger.info(
      `Sidecar plugin registered (url=${sidecarUrl}, account=${accountId})`
    );

    // ------------------------------------------------------------------
    // Fallback Routing (before_dispatch hook)
    // ------------------------------------------------------------------

    api.on("before_dispatch", async (event, ctx) => {
      // 1. Only intercept messages from the shared account
      if (ctx.accountId && ctx.accountId !== accountId) return;

      // 2. Only intercept messages routed to fallback agent
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
