# Migrating to model aliases and connections

This update requires a per-machine `workspace/models.yaml` for every enabled
Claude Code or Codex harness. There is no automatic conversion or fallback to
the old environment variables or CLI provider settings. Without this file,
Claude/Codex model listing and launches fail with a configuration error. pi is
unchanged.

Prepare each computer **before using Update & restart or pulling this change**.
The endpoint-only configuration on another computer is not a suitable default
for a machine using subscriptions. `localhost` always means the computer
running yuki-conductor.

## 1. Pause work and back up

Wait for active chat, automation, and task turns to finish. Pause the taskboard
and scheduled automations during maintenance, recording which ones to resume.
Stop the daemon before editing the database. On Windows, stop it through
yuki-watcher and disable its automatic relaunch for maintenance; on macOS/Linux,
stop the owning LaunchAgent/systemd service. A restart is not a maintenance stop.

All paths below are under `~/.yuki-conductor`, or the directory selected by
`YUKI_CONDUCTOR_DATA_DIR`. Back up `workspace/models.yaml` if present,
`workspace/cron.yaml`, and the private `.env`. Record the current git commit for
rollback. The script below creates a SQLite backup before changing selections.
Keep backups outside the repository and outside a synced workspace.

## 2. Describe this machine's existing route

Create `workspace/models.yaml`. Retain the current harness names and choose the
same models and route the machine already uses. You can prepare this file while
the old version is installed; the old version ignores it.

For a machine signed into Claude Code and Codex subscriptions, this complete
example preserves the familiar alias names:

```yaml
harness:
  claude-code:
    default: sonnet
    connections:
      account:
        type: subscription
    aliases:
      sonnet:
        model: sonnet
        connection: account
      opus:
        model: opus
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
      astra:
        model: gpt-6-astra
        connection: account
```

Include sections for enabled harnesses and any harness with saved selections
that the migration inventory reports. Ensure the real CLI's subscription login
is valid under the daemon's OS account. A subscription
connection explicitly selects the subscription route; existing endpoint env
variables and provider settings no longer choose the route.

For a gateway, replace the relevant connection with an explicit endpoint and
point the aliases at it:

```yaml
connections:
  gateway:
    type: endpoint
    base_url: http://localhost:4000
    api_key:
      file: ~/.yuki-conductor/.secrets/gateway-api-key
aliases:
  sonnet:
    model: claude-sonnet-5-5
    connection: gateway
```

This fragment belongs under `harness.claude-code`, with `default: sonnet`.
Claude needs an Anthropic Messages endpoint. Codex needs a Responses endpoint;
its `base_url` often ends in `/v1`, for example `http://localhost:4000/v1`.
Use the model IDs supported by that endpoint and preserve any required URL
prefix. Copy the route from the machine's current Claude settings/env or Codex
provider config; yuki will now override those settings for each launch.

Put the existing key into a private secret file, never into YAML. Restrict its
permissions to the daemon user (Unix: `chmod 600`; Windows: a user-only ACL).
Alternatively use `api_key: {env: GATEWAY_API_KEY}` and define that variable in
the daemon's private `.env`. A variable exported only in an interactive shell
may not reach the daemon. Do not copy secret files into the repo or the synced
workspace. The complete [example config](../../models.example.yaml) shows both
connection types.

Old `CLAUDE_DEFAULT_MODEL` and `CODEX_DEFAULT_MODEL` variables no longer set
defaults. Express their intended model as an alias and set that harness's
`default` to the alias ID. Remove those variables from the private `.env`.

## 3. Convert saved selections once

Selections in conversations, tasks (including completed/archived records), and
Slack pins now contain alias IDs. Null selections already follow the configured
default. Existing strings such as `gpt-6.1-sol` must become `sol` (or your chosen
alias ID). Keeping the old alias IDs `sonnet`/`opus` avoids changing those values.

Save the following as a private temporary `migrate-model-selections.py` outside
the repository. Run it from the checkout with
`uv run python /path/to/migrate-model-selections.py`. It first prints an inventory
and performs no writes. Edit `REMAP` for this machine, set `APPLY = True`, then run
it again. It checks every configured Claude/Codex selection before making any
changes, and refuses an unknown selection rather than guessing a route.

```python
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import yaml

APPLY = False
REMAP = {
    "claude-code": {"sonnet": "sonnet", "opus": "opus"},
    "codex": {"gpt-6.1-sol": "sol", "gpt-6-astra": "astra"},
}
root = Path(os.environ.get("YUKI_CONDUCTOR_DATA_DIR", Path.home() / ".yuki-conductor"))
workspace = root / "workspace"
config = yaml.safe_load((workspace / "models.yaml").read_text(encoding="utf-8"))["harness"]
db = workspace / "yuki-conductor.db"
if not db.is_file():
    raise SystemExit(f"Database does not exist: {db}")
con = sqlite3.connect(db)
tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
pending = []
errors = []
for table in ("conversations", "tasks", "models"):
    if table not in tables:
        continue
    if table == "models":
        rows = [(key, key.rsplit(":", 1)[-1], value)
                for key, value in con.execute("SELECT key, value FROM models")]
    else:
        rows = list(con.execute(f"SELECT id, harness, model FROM {table} WHERE model IS NOT NULL"))
    for row_id, harness, old in rows:
        if harness not in ("claude-code", "codex"):
            continue
        new = REMAP.get(harness, {}).get(old, old)
        print(table, row_id, harness, repr(old), "->", repr(new))
        if new not in config.get(harness, {}).get("aliases", {}):
            errors.append((table, row_id, harness, old))
        elif new != old:
            pending.append((table, row_id, new))
if errors:
    con.close()
    raise SystemExit(f"Unmapped selections; edit config or REMAP: {errors}")
if not APPLY:
    con.close()
    raise SystemExit("Inventory only. Review REMAP, then set APPLY = True.")
backup_dir = root / "backups"
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
backup = backup_dir / f"before-model-aliases-{stamp}.db"
with closing(sqlite3.connect(backup)) as dest:
    con.backup(dest)
with con:
    for table, row_id, new in pending:
        if table == "models":
            con.execute("UPDATE models SET value=? WHERE key=?", (new, row_id))
        else:
            con.execute(f"UPDATE {table} SET model=? WHERE id=?", (new, row_id))
con.close()
print(f"Updated {len(pending)} selections. Backup: {backup}")
```

For endpoint aliases named `sol-gh` or `sonnet-gh`, change the mapping targets
accordingly. Include any other values printed by the inventory, such as a
previously customized default model. Do not blindly map everything to the new
default: that can change the model or destination of existing conversations.

Also edit any explicit `model:` selections in `workspace/cron.yaml` to the new
alias IDs under the task's harness. Omitted models follow the configured default.
Keep the cron file's comments and prompt blocks intact. Do not change
`message_usage.model`, session IDs, message history, or historical run records:
those describe actual past runs, not future model selections.

## 4. Update, validate, and restart

With work still paused, update the checkout. Before starting the daemon, validate
every enabled Claude/Codex harness from the updated checkout:

```sh
uv run python -c "from yuki_conductor.harness import HARNESSES; from yuki_conductor.model_config import load_harness_models; [print(name, load_harness_models(name).default) for name in HARNESSES if name in ('claude-code', 'codex')]"
```

This checks configuration without reading key values. Start the daemon through
its normal supervisor and verify:

- The model picker lists this machine's aliases and defaults without errors.
- A new short test conversation works for each configured route.
- An existing conversation resumes with its original harness and intended model
  and endpoint. Review the selected alias before sending; changing a route sends
  the session's context to that endpoint.
- Explicit automation models and Slack channel pins refer to valid aliases.

Resume the automations and taskboard that were active before maintenance.
Changing `models.yaml` or a secret file later takes effect on subsequent launches;
changing the daemon's `.env` requires a restart.

## Recovery

If the update is already installed and model listing fails, stop new work, create
the configuration, and convert saved selections using the steps above. A missing
alias or key produces an error; it never silently switches to a subscription or
another endpoint.

To roll back, stop the daemon and supervisor first. Restore the previous checkout
and the matching database backup, cron file, and private `.env`. If restoring the
database while SQLite `-wal`/`-shm` sidecar files exist, move the current database
and its sidecars together into a separate recovery directory before placing the
backup at `workspace/yuki-conductor.db`. Keep all of them until recovery is
verified. Restoring a backup discards work recorded after that backup; choose a
maintenance window with no new writes. Restart through the normal supervisor.
