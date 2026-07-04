# Hermes Familiar host integration notes

This project now treats Hermes as the real host runtime, not a toy bridge. The current Hermes source tree (`~/.hermes/hermes-agent`) already exposes the integration surface needed by the familiar when the gateway API server is enabled:

- `POST /v1/runs` starts a server-side Hermes run.
- `GET /v1/runs/{run_id}` polls status.
- `GET /v1/runs/{run_id}/events` streams SSE lifecycle/tool/approval events.
- `POST /v1/runs/{run_id}/stop` interrupts a run.
- `POST /v1/runs/{run_id}/approval` resolves pending approvals.
- `/api/jobs/*` lists, creates, updates, deletes, pauses, resumes, and triggers scheduled cron jobs.
- `/v1/capabilities` advertises which of these features are available.

## Bridge modes

`scripts/hermes_serial_bridge.py` keeps the original read-only SQLite snapshot path for status and recent messages. Actions have two execution paths:

1. **Native API mode** (`--api-url` plus `--api-key`/`API_SERVER_KEY`): actions are real Hermes API runs or cron job controls. Run events are consumed from SSE and forwarded over the device protocol.
2. **CLI fallback** (no API credentials): actions use the configured local `command` subprocess. This preserves offline/local development and does not require secrets.

## Device protocol extension semantics

The firmware already accepts these host-to-device shapes:

```json
{"type":"state","running":1,"waiting":0,"mood":"thinking","job_state":"running","job_label":"Status brief"}
{"type":"event","event":"tool_started","msg":"terminal"}
{"type":"permission","id":"approval-id","text":"Hermes needs approval","choices":["once","deny"]}
{"type":"ack","msg":"started API: Status brief"}
```

The device sends:

```json
{"cmd":"action","action":"start"}
{"cmd":"action","action":"pause"}
{"cmd":"action","action":"cancel"}
{"cmd":"permission","decision":"once","id":"approval-id"}
```

The bridge maps these to `/v1/runs`, `/api/jobs`, or the CLI fallback depending on action config and API availability.

## Launchd helper

A launchd template is provided at:

```text
scripts/com.nous.hermes-familiar-bridge.plist.template
```

Install as a user LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cp scripts/com.nous.hermes-familiar-bridge.plist.template \
  ~/Library/LaunchAgents/com.nous.hermes-familiar-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nous.hermes-familiar-bridge.plist
launchctl kickstart -k gui/$(id -u)/com.nous.hermes-familiar-bridge
```

Check logs:

```bash
tail -f ~/Library/Logs/hermes-familiar-bridge.log ~/Library/Logs/hermes-familiar-bridge.err.log
```

Unload:

```bash
launchctl bootout gui/$(id -u)/com.nous.hermes-familiar-bridge
```

The template intentionally does not include `API_SERVER_KEY`. For native API mode, prefer starting Hermes gateway normally with its own configured API server and add `--api-url http://127.0.0.1:8642` to the plist only after confirming the gateway is healthy. Supply the key through your normal local secret mechanism or run the bridge manually while testing.
