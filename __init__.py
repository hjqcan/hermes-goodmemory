"""GoodMemory provider for Hermes Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider


logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8739"
_DEFAULT_RETRIEVAL_PROFILE = "general_chat"
_DEFAULT_MAX_TOKENS = 1200
_CONFIG_FILENAME = "goodmemory.json"


RECALL_SCHEMA = {
    "name": "goodmemory_recall",
    "description": (
        "Recall project-scoped GoodMemory context for a question. Returns the "
        "prompt-ready context, visible memory items, routing details, and trace ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Question or task to recall relevant memory for.",
            }
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "goodmemory_remember",
    "description": (
        "Store one durable, explicit, non-sensitive fact, preference, decision, "
        "or blocker in project-scoped GoodMemory. Do not store transcripts, "
        "credentials, private file contents, or unconfirmed inference."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "One durable statement to remember.",
            },
            "role": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": (
                    "Use user for user-provided facts and assistant only for an "
                    "agent conclusion that is confirmed or independently verified."
                ),
                "default": "user",
            },
        },
        "required": ["content"],
    },
}

REVISE_SCHEMA = {
    "name": "goodmemory_revise",
    "description": (
        "Correct a specific GoodMemory item by visible memory ID. Use only when "
        "the user supplies or confirms the correction target and replacement."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "Visible memory ID returned by GoodMemory recall.",
            },
            "content": {
                "type": "string",
                "description": "Corrected durable statement.",
            },
            "reason": {
                "type": "string",
                "description": "Why the existing memory is wrong or stale.",
            },
        },
        "required": ["memory_id", "content", "reason"],
    },
}

FORGET_SCHEMA = {
    "name": "goodmemory_forget",
    "description": (
        "Delete a specific GoodMemory item by visible memory ID. Use only for an "
        "explicit deletion request or a clearly invalid item."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "Visible memory ID returned by GoodMemory recall.",
            }
        },
        "required": ["memory_id"],
    },
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _error(code: str, message: str, *, status: Any = None) -> str:
    detail: Dict[str, Any] = {"code": code, "message": message}
    if isinstance(status, int):
        detail["status"] = status
    return json.dumps({"ok": False, "error": detail})


def _read_config(hermes_home: str) -> Dict[str, Any]:
    path = Path(hermes_home) / _CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("GoodMemory config could not be read: %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_cwd(explicit: Any) -> Path:
    value = _text(explicit)
    if not value:
        from agent.runtime_cwd import resolve_agent_cwd

        value = str(resolve_agent_cwd())
    return Path(value).expanduser().resolve()


def _derived_workspace_id(cwd: Path) -> str:
    digest = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:16]
    name = cwd.name or "workspace"
    return f"hermes:{name}:{digest}"


def _bridge_token() -> str:
    try:
        from agent.secret_scope import get_secret

        return get_secret("GOODMEMORY_BRIDGE_TOKEN", "") or ""
    except Exception:
        return os.environ.get("GOODMEMORY_BRIDGE_TOKEN", "")


class GoodMemoryMemoryProvider(MemoryProvider):
    """Project-scoped GoodMemory recall with explicit governed writes."""

    def __init__(self) -> None:
        self._client: Any = None
        self._scope: Any = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "goodmemory"

    def is_available(self) -> bool:
        try:
            import goodmemory_client  # noqa: F401

            return True
        except ImportError:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "base_url",
                "description": "GoodMemory HTTP bridge URL",
                "default": _DEFAULT_BASE_URL,
            },
            {
                "key": "token",
                "description": "GoodMemory bridge bearer token (blank for an explicitly insecure local bridge)",
                "secret": True,
                "required": False,
                "env_var": "GOODMEMORY_BRIDGE_TOKEN",
                "url": "https://github.com/hjqcan/GoodMemory#pythonfastapi-http-bridge",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        allowed = {
            "base_url",
            "user_id",
            "workspace_id",
            "retrieval_profile",
            "max_tokens",
            "timeout_seconds",
        }
        payload = {key: values[key] for key in allowed if key in values}
        path = Path(hermes_home) / _CONFIG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def initialize(self, session_id: str, **kwargs) -> None:
        from goodmemory_client import GoodMemoryClient, Scope

        hermes_home = str(kwargs.get("hermes_home", ""))
        self._config = _read_config(hermes_home)
        cwd = _resolve_cwd(kwargs.get("cwd"))

        user_id = _text(self._config.get("user_id"))
        if not user_id:
            user_id = _text(kwargs.get("user_id"))
        if not user_id:
            identity = _text(kwargs.get("agent_identity")) or "default"
            user_id = f"hermes:{identity}"

        workspace_id = _text(self._config.get("workspace_id"))
        if not workspace_id:
            workspace_id = _derived_workspace_id(cwd)

        self._scope = Scope(
            user_id=user_id,
            workspace_id=workspace_id,
            agent_id="hermes-agent",
        )
        base_url = _text(self._config.get("base_url")) or _DEFAULT_BASE_URL
        timeout = self._config.get("timeout_seconds", 6.0)
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError):
            timeout_seconds = 6.0

        self._client = GoodMemoryClient(
            base_url,
            scope=self._scope,
            token=_bridge_token() or None,
            operations=["recall-context", "remember", "revise", "forget"],
            timeout_seconds=timeout_seconds,
            max_attempts=2,
        )

    def system_prompt_block(self) -> str:
        return (
            "# GoodMemory\n"
            "GoodMemory automatically recalls project-scoped context from the "
            "configured bridge, which receives each non-trivial recall query. "
            "This provider does not automatically upload completed turns. Use "
            "goodmemory_remember only for one durable, explicit, non-sensitive "
            "fact, preference, decision, or blocker. Current code, tests, and the "
            "user's latest instruction override recalled memory. Use "
            "goodmemory_revise or goodmemory_forget only with a visible memory ID "
            "and a confirmed correction or deletion boundary."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._client or not _text(query):
            return ""
        try:
            result = self._client.recall_context(
                query,
                retrieval_profile=self._retrieval_profile(),
                output="system_prompt_fragment",
                max_tokens=self._max_tokens(),
            )
            return result.context_text if result.has_context else ""
        except Exception as exc:
            logger.warning("GoodMemory prefetch failed: %s", type(exc).__name__)
            return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, REMEMBER_SCHEMA, REVISE_SCHEMA, FORGET_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return _error("not_initialized", "GoodMemory provider is not initialized")
        try:
            if tool_name == "goodmemory_recall":
                query = _text(args.get("query"))
                if not query:
                    return _error("query_required", "query is required")
                result = self._client.recall_context(
                    query,
                    retrieval_profile=self._retrieval_profile(),
                    output="system_prompt_fragment",
                    max_tokens=self._max_tokens(),
                )
                return json.dumps(self._recall_payload(result))

            if tool_name == "goodmemory_remember":
                content = _text(args.get("content"))
                if not content:
                    return _error("content_required", "content is required")
                role = _text(args.get("role")) or "user"
                if role not in {"user", "assistant"}:
                    return _error("invalid_role", "role must be user or assistant")
                return json.dumps(
                    self._client.remember(
                        [{"role": role, "content": content}],
                        annotations=[
                            {
                                "messageIndex": 0,
                                "remember": "always",
                                "confirmed": True,
                                "reason": (
                                    "explicit Hermes goodmemory_remember tool call"
                                ),
                            }
                        ],
                    )
                )

            if tool_name == "goodmemory_revise":
                memory_id = _text(args.get("memory_id"))
                content = _text(args.get("content"))
                reason = _text(args.get("reason"))
                if not memory_id:
                    return _error("memory_id_required", "memory_id is required")
                if not content:
                    return _error("content_required", "content is required")
                if not reason:
                    return _error("reason_required", "reason is required")
                return json.dumps(
                    self._client.revise(
                        memory_id=memory_id,
                        content=content,
                        reason=reason,
                        idempotency_key=f"hermes:{uuid.uuid4()}",
                    )
                )

            if tool_name == "goodmemory_forget":
                memory_id = _text(args.get("memory_id"))
                if not memory_id:
                    return _error("memory_id_required", "memory_id is required")
                return json.dumps(self._client.forget(memory_id))

            return _error("unknown_tool", f"Unknown GoodMemory tool: {tool_name}")
        except Exception as exc:
            code = _text(getattr(exc, "code", "")) or "bridge_error"
            status = getattr(exc, "status", None)
            return _error(code, str(exc), status=status)

    def _retrieval_profile(self) -> str:
        return (
            _text(self._config.get("retrieval_profile")) or _DEFAULT_RETRIEVAL_PROFILE
        )

    def _max_tokens(self) -> int:
        value = self._config.get("max_tokens", _DEFAULT_MAX_TOKENS)
        try:
            return int(value)
        except (TypeError, ValueError):
            return _DEFAULT_MAX_TOKENS

    @staticmethod
    def _recall_payload(result: Any) -> Dict[str, Any]:
        routing = result.routing
        return {
            "ok": True,
            "context": result.context_text,
            "has_context": result.has_context,
            "item_count": result.item_count,
            "items": result.items,
            "routing": {
                "requested_strategy": routing.requested_strategy,
                "resolved_strategy": routing.resolved_strategy,
                "llm_refinement": routing.llm_refinement,
                "semantic_tie_breaking": routing.semantic_tie_breaking,
                "fallback_reason": routing.fallback_reason,
                "provider_fallback": routing.provider_fallback,
            },
            "contract_version": result.contract_version,
            "trace_id": result.trace_id,
        }


def register(ctx) -> None:
    ctx.register_memory_provider(GoodMemoryMemoryProvider())
