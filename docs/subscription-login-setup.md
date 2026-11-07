# Subscription login on another computer

Use this guide to configure yuki-conductor with Claude Code and Codex
subscription accounts on a new computer, including unattended runs while macOS
is locked. Complete the initial browser authorization yourself; subsequent
turns reuse the credentials.

## Prepare the computer

1. Install both CLIs and a yuki-conductor checkout containing subscription-token
   support and the Codex routing fixes. Follow the [main setup guide](../README.md)
   for installation and daemon management.
2. Run the login commands as the **same OS user that runs the daemon**.
3. Include the harnesses in `~/.yuki-conductor/.env`:

   ```dotenv
   HARNESSES=claude-code,codex
   ```

   Preserve any other enabled harnesses. Restart the daemon after changing this
   setting or updating backend code.

Paths below use the default data directory. If you set
`YUKI_CONDUCTOR_DATA_DIR`, use that directory for yuki-conductor's config and
secret files. Codex uses its own `CODEX_HOME` (default `~/.codex`); the login
commands and daemon must use the same Codex home. A saved Claude Code login
similarly uses `CLAUDE_CONFIG_DIR` (default `~/.claude`).

Keep credentials outside the repository and the synced `workspace/` directory.
Sign in on the new computer rather than copying another computer's auth cache.

## Claude Code: use a subscription token for unattended runs

Run:

```sh
claude setup-token
```

Complete the browser authorization with your Claude subscription account. The
command prints a long-lived OAuth token but does not save it. Save **only the
token**, on one line, in:

```text
~/.yuki-conductor/.secrets/claude-subscription-token
```

Use a local editor to save it; avoid commands containing the token that would
leave it in shell history. On macOS/Linux, create the directory first and then
restrict the saved file:

```sh
mkdir -p ~/.yuki-conductor/.secrets
chmod 700 ~/.yuki-conductor/.secrets
# Save the token in the file above before running the next command.
chmod 600 ~/.yuki-conductor/.secrets/claude-subscription-token
```

On Windows, `~` is your user profile directory. Restrict the file's ACL to the
daemon user; for a file saved at the default location, use PowerShell:

```powershell
icacls "$env:USERPROFILE\.yuki-conductor\.secrets\claude-subscription-token" /inheritance:r /grant:r "${env:USERNAME}:(F)"
```

The subscription connection in the YAML example below references this file.
Yuki reads it at each launch and passes it as `CLAUDE_CODE_OAUTH_TOKEN` in the
child environment. You do not need to export that variable in your shell or
put the token in `.env`. Missing, empty, or malformed files fail the launch
without falling back to a saved login. Endpoint connections still use their
own API keys.

Anthropic documents these tokens as lasting one year. Generate and replace the
token when it expires or is revoked; the next turn reads the replacement.
Tokens support model requests and local MCP servers, but not Remote Control
sessions or claude.ai connectors.

For normal saved-account login instead, run `claude auth login` and omit
`oauth_token` from the subscription connection. On macOS, that login can depend
on Keychain access; use the token option for unattended operation.

## Codex: save the ChatGPT login in a file

Edit `~/.codex/config.toml` and set this key at the **top level**, before any
`[table]` headers:

```toml
cli_auth_credentials_store = "file"
```

Keep other provider configuration, including LiteLLM. Remove any user-configured
`forced_login_method = "api"` restriction: it conflicts with saved ChatGPT
credentials and can cause Codex to log out and delete them. Organization-managed
restrictions require your administrator to allow the intended login.

Then run:

```sh
codex login
codex login status
```

Complete the browser login with your ChatGPT account. Status should report
`Logged in using ChatGPT`. If the computer cannot open a browser or receive the
local callback, use `codex login --device-auth` and authorize the displayed code
in a browser on another device. Device-code login must be enabled in your
ChatGPT account or workspace settings.

Credentials are saved in `CODEX_HOME/auth.json`. On macOS/Linux:

```sh
chmod 600 ~/.codex/auth.json
```

On Windows, restrict the file's ACL to the daemon user:

```powershell
icacls "$env:USERPROFILE\.codex\auth.json" /inheritance:r /grant:r "${env:USERNAME}:(F)"
```

Adjust these commands if you use a custom Codex home. The daemon user needs
write access: Codex automatically refreshes ChatGPT tokens during use and saves
the updated credentials. If refresh fails or login is revoked, run `codex login`
again. Treat `auth.json` as a secret.

## Configure subscription model aliases

Create or edit `~/.yuki-conductor/workspace/models.yaml`. This minimal example
enables both subscriptions:

```yaml
harness:
  claude-code:
    default: sonnet
    connections:
      account:
        type: subscription
        oauth_token:
          file: ~/.yuki-conductor/.secrets/claude-subscription-token
    aliases:
      sonnet:
        model: sonnet
        connection: account
  codex:
    default: sol
    connections:
      account:
        type: subscription
    aliases:
      sol:
        model: gpt-6.1-sol
        connection: account
```

Use model names available to your account. If you already have aliases or
LiteLLM connections, add the subscription connections and aliases to the
existing file instead of replacing it. Keep existing alias IDs that saved
conversations, tasks, and automations use. The complete
[model example](../models.example.yaml) shows subscription and endpoint aliases
together.

Subscription connections have no `base_url` or `api_key`. Codex subscription
turns use the built-in OpenAI provider at
`https://chatgpt.com/backend-api/codex` with the saved ChatGPT login. Codex
endpoint turns use their configured URL and API key independently of that
login. File credential storage does not change LiteLLM routing.

Config and token-file edits apply to subsequent turns without a daemon restart.
No new conversation is required for a model change within the same harness;
the next turn resumes its session with the selected model's connection.

## Verify through yuki-conductor

1. Open a Claude Code conversation, select the subscription alias explicitly
   (`sonnet` in the example), and ask it to reply `OK` without using tools.
2. Do the same in a Codex conversation using `sol`.
3. If you also use LiteLLM, select an endpoint alias and repeat. Confirm those
   requests appear in LiteLLM's request logs.
4. For unattended macOS use, lock the screen and send another message from a
   second device to each subscription conversation. Keep the Mac awake and
   connected to the network.

`codex login status` describes the saved login, not the route of a particular
Yuki turn. The selected alias's `connection` in `models.yaml` determines that
route. Likewise, standalone `claude auth status` does not check the token file
Yuki injects at launch.

Locking the screen leaves the macOS login session and LaunchAgent running.
Logging out or rebooting before a user signs in is different: Yuki's macOS
LaunchAgent starts in a user login session, and FileVault may require an unlock
after reboot. Token/file authentication does not change those startup rules.

## Troubleshooting

- **Codex says API key login is required and deletes `auth.json`:** update Yuki
  to include the endpoint-login fix and restart it. Remove a conflicting
  user-configured forced API login restriction, then run `codex login` again.
- **Codex subscription gets `Missing scopes: api.responses.write` at
  `https://api.openai.com/v1/responses`:** the subscription request is reaching
  the API endpoint. Update Yuki and restart; subscription launches must use
  the ChatGPT Codex endpoint above.
- **Claude cannot read the subscription token:** check the configured path,
  daemon user's file permissions, and that the file contains only the token.
  A shell export alone is insufficient; configure `oauth_token.file`.
- **A login works in a terminal but fails in Yuki:** confirm the daemon runs as
  that OS user, uses the same CLI home, and has restarted after backend or
  harness-enable configuration changes.

See [Claude Code authentication](https://code.claude.com/docs/en/authentication)
and the [official OpenAI authentication documentation](https://developers.openai.com/codex/auth/)
for credential storage, token lifetime, and headless login details.
