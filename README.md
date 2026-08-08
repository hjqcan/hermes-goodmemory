# GoodMemory for Hermes Agent

A standalone Hermes `MemoryProvider` for
[GoodMemory](https://github.com/hjqcan/GoodMemory). It recalls scoped context
before non-trivial turns and exposes explicit tools for remembering, correcting,
and deleting individual memories.

The provider does not upload completed conversations automatically. When the
configured bridge is remote, recall queries and explicit tool inputs leave the
machine and are sent to that bridge.

## Install

Prerequisites:

- Hermes Agent with the external `MemoryProvider` interface
- A GoodMemory HTTP bridge using contract `phase-39.http-memory.v1`
- Python 3.11+

Install the plugin, configure its provider, and start a new session:

```bash
hermes plugins install hjqcan/hermes-goodmemory --no-enable
hermes memory setup   # select goodmemory
hermes memory status
```

The setup command installs the pinned `goodmemory-client` dependency, asks for
the bridge URL, stores the bearer token in the active Hermes profile's `.env`,
and activates `memory.provider: goodmemory`.

For a local bridge:

```bash
npm install -g goodmemory@0.7.3
GOODMEMORY_HTTP_BRIDGE_ALLOW_INSECURE=1 \
  goodmemory-http-bridge --host 127.0.0.1 --port 8739 --recommended
```

Use `http://127.0.0.1:8739` during `hermes memory setup` and leave
the token blank only for this explicitly insecure loopback setup.

GoodMemory is also deployed at `https://goodmemory.vibenest.net`; that hosted
instance requires a bearer token issued by its deployment owner. The plugin
does not contain or distribute that credential.

## Scope and privacy

- The user scope defaults to the Hermes profile identity. Gateway user IDs are
  used when Hermes supplies one.
- The workspace scope is a deterministic, non-reversible hash of Hermes' actual
  runtime working directory. The absolute path is not sent to the bridge.
- Session IDs are intentionally excluded from the GoodMemory scope so the same
  user and workspace can recall across `/new` sessions.
- Different working directories derive different workspace IDs.
- Each non-trivial turn can send its recall query to the configured bridge.
- Completed turns, raw transcripts, tool outputs, files, and credentials are
  not automatically written.

Advanced scope overrides can be placed in the active profile's
`goodmemory.json`:

```json
{
  "base_url": "http://127.0.0.1:8739",
  "user_id": "my-stable-user",
  "workspace_id": "my-stable-workspace",
  "retrieval_profile": "general_chat",
  "max_tokens": 1200,
  "timeout_seconds": 6
}
```

Do not put the bearer token in this JSON file. Hermes stores it as
`GOODMEMORY_BRIDGE_TOKEN` in the active profile's `.env`.

## Tools

| Tool | Boundary |
|---|---|
| `goodmemory_recall` | Returns context, visible items, routing, and trace ID. |
| `goodmemory_remember` | Writes one explicit durable statement with user/assistant provenance. |
| `goodmemory_revise` | Corrects a visible memory ID with a reason. |
| `goodmemory_forget` | Deletes a visible memory ID. |

Current code, tests, and the user's latest instruction take precedence over
recalled memory. Do not store secrets, credentials, full transcripts, private
file contents, or unconfirmed inference.

## Verification

Run the unit and current-Hermes contract tests from this repository:

```bash
HERMES_AGENT_REPO=/path/to/hermes-agent \
PYTHONPATH=/path/to/hermes-agent \
  uv run --with 'pytest>=8,<9' --with 'pyyaml>=6,<7' \
  pytest tests -q
```

With a disposable or dedicated bridge, run the real write/recall/correct/delete
proof:

```bash
GOODMEMORY_LIVE_URL=http://127.0.0.1:8739 \
GOODMEMORY_BRIDGE_TOKEN=your-token \
PYTHONPATH=/path/to/hermes-agent \
  uv run python tests/live_smoke.py
```

Hermes 0.20 currently has upstream reports affecting some provider surfaces:

- [#76231](https://github.com/NousResearch/hermes-agent/issues/76231): provider
  initialization omits `cwd`. This plugin resolves Hermes' canonical runtime
  cwd directly, so its workspace scope does not use the process fallback.
- [#79339](https://github.com/NousResearch/hermes-agent/issues/79339):
  `sync_turn()` can be skipped. This plugin does not depend on automatic turn
  syncing; writes are explicit tools.
- [#81427](https://github.com/NousResearch/hermes-agent/issues/81427): provider
  tools may be absent in Desktop sessions. CLI is the supported verification
  surface until that upstream issue is fixed; automatic recall may still work.

These are upstream limitations, not claims that every Hermes surface has been
verified.

## Report a real install

Use the structured
[Hermes install or usage report](https://github.com/hjqcan/hermes-goodmemory/issues/new?template=install-report.yml)
after a real run. Successful and failed reports are both useful when they include
the Hermes and plugin versions, platform, bridge mode, exact commands, and
sanitized output. Never include tokens or real memory content.
