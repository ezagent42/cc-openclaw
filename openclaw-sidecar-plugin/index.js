/**
 * OpenClaw Sidecar Routing Plugin
 *
 * Intercepts messages routed to the fallback agent (no peer binding match)
 * and calls the Sidecar API to provision/restore/deny — without invoking
 * the LLM.  Uses the `before_dispatch` hook which fires after routing
 * but before agent invocation.
 */

const SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791";
const DEFAULT_ACCOUNT_ID = "shared";

export default {
  id: "openclaw-sidecar",
  name: "Sidecar Routing",
  description: "Routes unmatched messages via Sidecar API",

  register(api) {
    const cfg = api.pluginConfig || {};
    const sidecarUrl = cfg.sidecarUrl || SIDECAR_DEFAULT_URL;
    const accountId = cfg.accountId || DEFAULT_ACCOUNT_ID;

    api.logger.info(`Sidecar plugin registered (url=${sidecarUrl}, account=${accountId})`);

    api.on("before_dispatch", async (event, ctx) => {
      // 1. Only intercept messages from the shared account
      if (ctx.accountId && ctx.accountId !== accountId) return;

      // 2. Only intercept messages routed to fallback agent
      //    sessionKey format: "agent:<agentId>:<channel>:..."
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

          if (action === "active") {
            // Group agent exists — let normal routing handle it
            return;
          }

          if (action === "provision_group") {
            const provResp = await fetch(`${sidecarUrl}/api/v1/provision-group`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ chat_id: conversationId }),
            });
            if (provResp.ok) {
              api.logger.info(`Provisioned group agent for ${conversationId}`);
              return {
                handled: true,
                text: "群助手正在准备中，请稍后再 @ 我",
              };
            }
            api.logger.error(`provision-group failed: ${provResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          // Other actions (deny, retry_later, etc.) — don't intercept
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
        // 3. Call resolve-sender
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

        // 4. Act on the decision
        switch (action) {
          case "provision": {
            const provResp = await fetch(`${sidecarUrl}/api/v1/provision`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ open_id: senderId }),
            });
            if (provResp.ok) {
              api.logger.info(`Provisioned agent for ${senderId}`);
              return {
                handled: true,
                text: "正在为您准备专属助手，请再发一条消息开始对话",
              };
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
              return {
                handled: true,
                text: "您的助手已恢复，请再发一条消息继续对话",
              };
            }
            api.logger.error(`restore failed: ${restResp.status}`);
            return { handled: true, text: "系统维护中，请稍后重试" };
          }

          case "deny":
            return { handled: true, text: message || "您没有权限使用本助手，如需使用请联系管理员" };

          case "deny_silent":
            return { handled: true }; // no text = silent

          case "retry_later":
            return { handled: true, text: "您的助手正在准备中，请稍后再试" };

          default:
            // Unknown action — don't intercept, let fallback handle
            api.logger.warn(`Unknown action: ${action}`);
            return;
        }
      } catch (err) {
        api.logger.error(`Sidecar API error: ${err.message}`);
        // Sidecar down — let fallback agent handle (degraded)
        return;
      }
    });
  },
};
