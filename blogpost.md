# Managing OpenClaw with Claude Code: From Ad-Hoc Config to Standardized Operations

*This is a follow-up to [OpenClaw in the Real World](https://trilogyai.substack.com/p/openclaw-in-the-real-world), where we covered the patterns that emerge when you move AI agents from experiments to production — memory architecture, deterministic scripts, Git+Stow deployment, and the shift from prompting to configuration. This post is about what happens next: making those patterns repeatable.*

---

## The Configuration Problem Nobody Talks About

In the last article, I described what a production OpenClaw deployment looks like after a few months: 15 agents, 23 cron jobs, 4 Telegram bots, 2 Slack workspaces, a WhatsApp integration, macOS Keychain secrets, deterministic scripts, dream routines, and a Git+Stow deployment pipeline.

What I didn't talk about was how all that configuration actually gets created.

The honest answer? Ad hoc. Every single time.

You want a new agent? You open `openclaw.json` in your editor, scroll through 800 lines of JSON, find `agents.list`, copy an existing entry, change the fields, hope you didn't miss a comma, create the directory structure from memory, write six markdown files, run stow, restart the gateway, and pray.

You want to add a Telegram bot? You create the bot in BotFather, copy the token, figure out the keychain command syntax (`security add-generic-password -s ... -a ... -w ...`), remember that you also need to update `openclaw-secrets.sh` *and* `openclaw-env.sh` *and* `secrets.sh`, add the channel config to `openclaw.json`, create the binding, stow, restart, check logs.

You want a cron job? You generate a UUID, construct the JSON object from memory, get the cron expression right, remember that `tz` defaults to UTC if you forget it, add it to `jobs.json`, remember that the gateway overwrites `jobs.json` as a real file so you need to `rm` it before stowing...

Every one of these operations is documented. The patterns are established. But each time you do one, you're reconstructing it from scratch. And every reconstruction is an opportunity to forget a step, miss a file, or introduce a subtle misconfiguration that doesn't surface until 3 AM when an agent silently stops receiving messages.

This is the gap between having good patterns and actually *following* them consistently.

## Why OpenClaw Configuration Is Inherently Ad Hoc

This isn't a design flaw — it's a consequence of how OpenClaw works, and it's actually one of its strengths.

OpenClaw is configuration-driven. Everything lives in flat files: JSON config, markdown directives, shell scripts, keychain entries. There's no web UI, no database, no admin panel. The entire state of your deployment is a directory tree you can `ls`.

This is *exactly* what makes Git+Stow viable. It's why disaster recovery takes 10 minutes instead of 10 hours. It's why you can diff two agent configurations, branch experimental changes, and roll back a broken deploy with `git checkout`.

But flat-file configuration also means there's no workflow engine forcing you through steps in order. Nothing validates that your `openclaw.json` entry matches your directory structure. Nothing checks that you updated all three secrets files when you added a keychain entry. Nothing reminds you that the gateway needs a restart after a stow, or that `jobs.json` needs to be removed first because the gateway overwrote the symlink.

You are the workflow engine. And you're running on biological hardware that forgets steps, gets interrupted, and occasionally transposes characters in UUIDs.

## Enter Claude Code Skills

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) has a feature called [skills](https://docs.anthropic.com/en/docs/claude-code/skills) — markdown files that live in `.claude/skills/` in your project. Each skill is a playbook: a structured set of steps that Claude Code follows when you invoke it with a slash command.

The key insight: Claude Code skills are the perfect middle ground between a manual runbook and a custom CLI tool.

- They're **not code** — they're markdown instructions. You can read them, edit them, version control them. No build step, no dependencies, no binary to maintain.
- They're **not documentation** — they're executable. When you type `/openclaw-new-agent pappu-jr "Pappu Junior"`, Claude Code reads the skill and *does the thing*. Creates directories, generates files, edits JSON, runs stow, restarts the gateway.
- They **encode institutional knowledge** — the fact that you need to `rm -f ~/.openclaw/cron/jobs.json` before stowing because the gateway overwrites it. The naming convention for keychain services (`openclaw.<service-name>`, lowercase, hyphens) vs. environment variables (`OPENCLAW_<SERVICE_NAME>`, uppercase, underscores). The six files every agent needs. The three files every secret touches.

The result is that configuring OpenClaw becomes a conversation instead of an archaeology expedition through your own setup.

## The Skills

We've open-sourced a set of nine Claude Code skills at [**cc-openclaw**](https://github.com/rahulsub-be/cc-openclaw). Here's what they do and why each one exists.

### `/openclaw-new-agent` — Because Agents Are More Than a JSON Entry

Creating an agent in OpenClaw means touching at least seven things: the agent entry in `openclaw.json`, the directory tree, and five or six markdown directive files (`SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`, `SECURITY.md`).

Skip `SECURITY.md` and your agent has no credential handling policy. Forget the `memory/archives/` subdirectory and the dream routine will fail silently three weeks later when it first tries to archive a distillation. Miss the `scripts/lib/` directory and deterministic script scaffolding breaks.

The skill handles all of it. It also asks the right questions upfront: Is this a standalone agent or a sub-agent? If sub-agent, which parent? What model should it use? Does it need a channel? Then it generates everything from templates drawn from production agents.

**Maps to best practice:** Agent Hierarchy & Workspace Organization (Part 2 of the original article). The skill enforces the file discipline pattern — core config is version-controlled, generated content is tracked, ephemeral data is gitignored.

### `/openclaw-add-channel` — Because Secrets Have a Pipeline

Adding a messaging channel isn't just config. It's config *and* secrets *and* routing.

For Telegram alone, the pipeline is: BotFather token → macOS Keychain → `openclaw-secrets.sh` (for launchd) → `openclaw-env.sh` (for shell sessions) → `secrets.sh` (for provisioning new machines) → `openclaw.json` channel config → `openclaw.json` binding → stow → gateway restart → log verification.

Miss the `openclaw-env.sh` step and your CLI commands fail with `MissingEnvVarError` while the gateway works fine (because it uses `openclaw-secrets.sh`). Miss `secrets.sh` and your disaster recovery script won't provision the token on a fresh machine.

The skill handles each platform differently — Telegram needs a bot token, Slack needs both a bot token and an app token for socket mode, WhatsApp uses phone number allowlists — and routes through the full secrets pipeline every time.

**Maps to best practice:** Security Model (Part 5 of the original article). Keychain-only secrets, never written to files, consistent naming conventions enforced by the skill.

### `/openclaw-add-cron` — Because Cron > Heartbeat

The original article made the case for cron over heartbeat: system-managed scheduling guarantees execution independent of agent workload. This skill makes it trivial to act on that principle.

It supports all three schedule types (`cron` for recurring, `every` for intervals, `at` for one-shots), handles the UUID generation, sets appropriate timeouts, and always uses isolated sessions by default (cheaper, cleaner, no context bleed from the agent's main conversation).

It also knows about the `jobs.json` gotcha — the gateway overwrites this file on every startup, turning your stow symlink into a real file. The skill handles `rm → stow` automatically.

**Maps to best practice:** Determinism Over Prompting (Part 4 of the original article). Cron jobs with deterministic scripts eliminate the "did the agent remember to check?" failure mode.

### `/openclaw-dream-setup` — Because Memory Doesn't Maintain Itself

Dream routines — nightly memory distillation — were one of the most impactful patterns from the original article. They're also one of the most complex to set up correctly.

A working dream routine requires: `DREAM-ROUTINE.md` (the distillation spec with token budgets), `MEMORY.md` (the curated long-term knowledge base), the `memory/archives/` directory, a cron job, QMD index paths in `openclaw.json`, and an updated session startup sequence in `AGENTS.md`.

The token budgets matter. 2,500 tokens per daily distillation, 7,500 for the rolling 3-day digest. Blow these and your agent's context window fills with memory retrieval instead of actual work. The skill encodes these constraints directly.

**Maps to best practice:** Memory Architecture That Scales (Part 1 of the original article). QMD, dream routines, and the transaction vs. operational memory distinction.

### `/openclaw-add-script` — Because Scripts Are Tools

The original article's mantra: "Reserve LLM capacity for interpreting intent." Deterministic scripts handle the compute-heavy, structured-output tasks. The LLM orchestrates.

But the script pattern has ceremony: `set -euo pipefail`, the `json-response.sh` shared library, stdout-only-JSON, stderr-for-logging, exit-code-is-law. Writing a new script from scratch means either copying an existing one and modifying it (introducing drift) or remembering all the conventions.

The skill scaffolds correctly every time. It creates the shared library if it doesn't exist, generates the script from a template, asks what the script should do, implements the logic, makes it executable, and documents it in `TOOLS.md`.

**Maps to best practice:** Determinism Over Prompting (Part 4). Scripts as workers with structured JSON output.

### `/openclaw-add-secret` — Because Three Files, Every Time

A secret in OpenClaw touches three files beyond the keychain itself:

1. `openclaw-secrets.sh` — loaded by launchd when the gateway starts
2. `openclaw-env.sh` — sourced by your shell for CLI commands
3. `secrets.sh` — the provisioning script for setting up a fresh machine

Forget file 2 and you get `MissingEnvVarError` in your terminal while the gateway works fine. Forget file 3 and your 10-minute disaster recovery becomes a "which secrets am I missing?" puzzle.

The skill enforces the naming conventions (keychain service: `openclaw.<name>`, lowercase hyphens; env var: `OPENCLAW_<NAME>`, uppercase underscores) and updates all three files automatically. It never echoes the secret value back — not in the terminal, not in a file, not in git history.

**Maps to best practice:** Security Model. Keychain-first, convention-enforced, provisioning-aware.

### `/openclaw-status` — Because You Need a Dashboard

When something's wrong — an agent isn't responding, messages aren't arriving, a cron job stopped firing — the first five minutes are spent figuring out *what's* wrong. Is the gateway running? Are channels connected? Is WhatsApp in a restart loop? Did a cron job start failing silently?

The skill checks everything in one pass: gateway health endpoint, launchd service status, channel connectivity from logs (Telegram bots, Slack socket mode, WhatsApp auth), agent count, cron job results (last run, consecutive errors), and recent error log entries.

It's the `kubectl get pods` equivalent for OpenClaw.

**Maps to best practice:** This one maps to operational maturity generally. The original article emphasized that agents need monitoring infrastructure — this skill *is* that monitoring, available on demand.

### `/openclaw-restart` and `/openclaw-stow` — Because Operations Have Gotchas

These are the simplest skills, and that's the point.

Restarting the OpenClaw gateway isn't `launchctl kickstart`. It's `rm -f ~/.openclaw/cron/jobs.json` → stow → kickstart → wait → verify channels. The `jobs.json` conflict is the kind of operational knowledge that lives in one person's head and costs 20 minutes of debugging when someone else encounters it for the first time.

The restart skill also verifies that channels actually reconnect — it doesn't just fire-and-forget the launchctl command. It checks logs for Telegram bot startup, Slack socket connection, and WhatsApp auth state.

**Maps to best practice:** Git+Stow Deployment (Part 3 of the original article). Stow is the deployment mechanism, and the skill encodes its operational quirks.

## The Meta-Pattern: Agents Managing Agent Infrastructure

There's something philosophically satisfying about using an AI coding assistant to manage AI agent infrastructure. But the practical argument is stronger.

OpenClaw's flat-file, configuration-driven architecture is *perfect* for LLM-assisted management. Every operation is: read some files, make some edits, run some commands. That's exactly what Claude Code does. The skills just tell it *which* files, *which* edits, and *which* commands — with all the institutional knowledge baked in.

This is different from building a CLI tool or a web admin panel:

- **A CLI tool** needs to be maintained as the config schema evolves. Skills are markdown — you edit them in five seconds.
- **A web panel** adds a server, a database, authentication, and a deployment pipeline for the admin tool itself. Skills are files in a git repo.
- **Documentation** tells you what to do. Skills *do it*, while explaining what they're doing so you can verify and learn.

The skills also compose naturally. `/openclaw-new-agent` suggests running `/openclaw-add-channel` if you want messaging. `/openclaw-add-channel` calls the same secrets pipeline as `/openclaw-add-secret`. `/openclaw-dream-setup` creates a cron job the same way `/openclaw-add-cron` does. Each skill is independent, but they share the same operational patterns.

## Getting Started

The skills are open-source at [**github.com/rahulsub-be/cc-openclaw**](https://github.com/rahulsub-be/cc-openclaw).

To use them, clone the repo and copy the skills into your openclaw-home project:

```bash
cd ~/your-openclaw-home-repo
git clone https://github.com/rahulsub-be/cc-openclaw.git .cc-openclaw-tmp
cp -r .cc-openclaw-tmp/.claude/skills/openclaw-* .claude/skills/
rm -rf .cc-openclaw-tmp
```

Open Claude Code in your openclaw-home directory and the skills auto-discover. Type `/openclaw-` and you'll see all nine in autocomplete.

All skills detect your repo location automatically via the stow symlink:

```bash
OPENCLAW_REPO=$(readlink ~/.openclaw/openclaw.json 2>/dev/null | sed 's|/.openclaw/openclaw.json||')
```

No hardcoded paths. Works regardless of where you've cloned your repo.

## What's Next

These skills encode the patterns from one production deployment. They're opinionated — they assume macOS, GNU Stow, macOS Keychain, and the specific file conventions described in the original article.

The contribution model is simple: each skill is a single markdown file. If your deployment has different conventions — Linux Keyring instead of macOS Keychain, systemd instead of launchd, a different directory structure — fork the repo and adapt the skills. The format is designed to be readable and editable by humans.

We're also working on companion documentation playbooks that go deeper into each pattern: memory system design, sub-agent architecture, the monitor-devbot pattern for autonomous development. These will be published separately and cross-referenced from the skills.

The end goal is straightforward: make it so that running a fleet of AI agents is as operationally boring as running any other production system. Standardized setup, consistent configuration, reproducible operations. No tribal knowledge. No archaeology.

Just type the slash command.

---

*The [cc-openclaw](https://github.com/rahulsub-be/cc-openclaw) repo is MIT-licensed. PRs welcome.*

*This article is part of the [Trilogy AI Center of Excellence](https://trilogyai.substack.com/) series on production AI agent infrastructure.*
