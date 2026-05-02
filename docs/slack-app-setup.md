# Slack App Setup Guide

## 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** > **From scratch**
3. Name it (e.g., "Claude Code Bot") and select your workspace

## 2. Enable Socket Mode

1. Go to **Settings** > **Socket Mode**
2. Toggle **Enable Socket Mode** on
3. Create an app-level token with the `connections:write` scope
4. Save the token as `SLACK_APP_TOKEN` (starts with `xapp-`)

## 3. Add Bot Scopes

Go to **OAuth & Permissions** > **Scopes** > **Bot Token Scopes** and add:

- `chat:write` — Send messages
- `channels:history` — Read public channel messages
- `groups:history` — Read private channel messages
- `im:history` — Read DMs
- `mpim:history` — Read group DMs
- `reactions:write` — Add/remove emoji reactions
- `commands` — Add slash commands

## 4. Add Slash Commands

Go to **Features** > **Slash Commands** > **Create New Command**:

| Field | Value |
|-------|-------|
| Command | `/yuki-model` |
| Short Description | Switch Claude model (sonnet, opus, haiku) |
| Usage Hint | `[sonnet\|opus\|haiku]` |

No **Request URL** is needed — Socket Mode handles delivery automatically.

## 5. Subscribe to Events

Go to **Event Subscriptions** > **Subscribe to bot events** and add:

- `message.channels` — Messages in public channels
- `message.groups` — Messages in private channels
- `message.im` — Direct messages
- `message.mpim` — Group direct messages

## 6. Install to Workspace

1. Go to **OAuth & Permissions**
2. Click **Install to Workspace** and authorize
3. Copy the **Bot User OAuth Token** as `SLACK_BOT_TOKEN` (starts with `xoxb-`)

> **Note:** If you added scopes or commands after the initial install, you must
> **reinstall the app** for the changes to take effect.

## 7. Configure Environment

Create a `.env` file in the project root:

```
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_APP_TOKEN=xapp-your-token
```

## 8. Invite the Bot

Invite the bot to a channel: `/invite @Claude Code Bot`
