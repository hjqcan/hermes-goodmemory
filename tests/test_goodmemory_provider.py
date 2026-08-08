from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HERMES_REPO = Path(
    os.environ.get("HERMES_AGENT_REPO", "/tmp/hermes-goodmemory.qITSHd")
).resolve()


class FakeScope:
    def __init__(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.agent_id = agent_id
        self.session_id = session_id


class FakeRouting:
    requested_strategy = "hybrid"
    resolved_strategy = "hybrid"
    llm_refinement = False
    semantic_tie_breaking = True
    fallback_reason = None
    provider_fallback = None


class FakeRecallResult:
    context_text = "Project deploys from the release tag."
    has_context = True
    item_count = 1
    items = [
        {
            "memoryId": "m-1",
            "content": "Project deploys from the release tag.",
            "type": "decision",
        }
    ]
    routing = FakeRouting()
    contract_version = "phase-39.http-memory.v1"
    trace_id = "trace-1"


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(
        self,
        base_url: str,
        *,
        scope: FakeScope,
        token: str | None = None,
        operations="*",
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        self.base_url = base_url
        self.scope = scope
        self.token = token
        self.operations = operations
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.calls: list[tuple] = []
        self.__class__.instances.append(self)

    def recall_context(self, query: str, **kwargs):
        self.calls.append(("recall_context", query, kwargs))
        return FakeRecallResult()

    def remember(self, messages, **kwargs):
        self.calls.append(("remember", list(messages), kwargs))
        return {"ok": True, "operation": "remember", "accepted": 1}

    def forget(self, memory_id: str, **kwargs):
        self.calls.append(("forget", memory_id, kwargs))
        return {"ok": True, "operation": "forget", "memoryId": memory_id}

    def revise(self, **kwargs):
        self.calls.append(("revise", kwargs))
        return {"ok": True, "operation": "revise", "memoryId": kwargs["memory_id"]}


@pytest.fixture
def plugin(monkeypatch):
    if not (HERMES_REPO / "agent" / "memory_provider.py").exists():
        pytest.skip(f"Hermes Agent checkout not found: {HERMES_REPO}")

    monkeypatch.syspath_prepend(str(HERMES_REPO))
    fake_client_module = types.ModuleType("goodmemory_client")
    fake_client_module.GoodMemoryClient = FakeClient
    fake_client_module.Scope = FakeScope
    monkeypatch.setitem(sys.modules, "goodmemory_client", fake_client_module)
    FakeClient.instances.clear()

    module_name = "hermes_goodmemory_test_plugin"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def initialize_provider(plugin, tmp_path: Path, monkeypatch, **kwargs):
    monkeypatch.setenv("GOODMEMORY_BRIDGE_TOKEN", "test-token")
    provider = plugin.GoodMemoryMemoryProvider()
    provider.initialize(
        session_id=kwargs.pop("session_id", "session-a"),
        hermes_home=str(tmp_path),
        platform="cli",
        agent_identity=kwargs.pop("agent_identity", "coder"),
        cwd=kwargs.pop("cwd", str(tmp_path / "project-a")),
        **kwargs,
    )
    return provider, FakeClient.instances[-1]


def test_manifest_is_installable_and_pins_the_bridge_client():
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "goodmemory"
    assert manifest["version"] == "0.1.0"
    assert manifest["pip_dependencies"] == ["goodmemory-client>=0.1.0,<0.3"]
    assert manifest["requires_env"][0]["name"] == "GOODMEMORY_BRIDGE_TOKEN"
    assert manifest["requires_env"][0]["secret"] is True


def test_registers_one_provider_with_four_explicit_tools(plugin):
    registered = []
    plugin.register(types.SimpleNamespace(register_memory_provider=registered.append))

    assert len(registered) == 1
    provider = registered[0]
    assert provider.name == "goodmemory"
    assert [schema["name"] for schema in provider.get_tool_schemas()] == [
        "goodmemory_recall",
        "goodmemory_remember",
        "goodmemory_revise",
        "goodmemory_forget",
    ]


def test_scope_is_stable_across_sessions_and_isolated_by_workspace(
    plugin, tmp_path, monkeypatch
):
    first, first_client = initialize_provider(
        plugin,
        tmp_path,
        monkeypatch,
        session_id="session-a",
        cwd=str(tmp_path / "project-a"),
    )
    second, second_client = initialize_provider(
        plugin,
        tmp_path,
        monkeypatch,
        session_id="session-b",
        cwd=str(tmp_path / "project-a"),
    )
    third, third_client = initialize_provider(
        plugin,
        tmp_path,
        monkeypatch,
        session_id="session-c",
        cwd=str(tmp_path / "project-b"),
    )

    assert first_client.scope.user_id == "hermes:coder"
    assert first_client.scope.agent_id == "hermes-agent"
    assert first_client.scope.session_id is None
    assert first_client.scope.workspace_id == second_client.scope.workspace_id
    assert first_client.scope.workspace_id != third_client.scope.workspace_id
    assert first_client.scope.workspace_id.startswith("hermes:project-a:")

    first.shutdown()
    second.shutdown()
    third.shutdown()


def test_scope_uses_hermes_runtime_cwd_when_initialize_kwarg_is_missing(
    plugin, tmp_path, monkeypatch
):
    runtime_project = tmp_path / "desktop-project"
    runtime_project.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(runtime_project))
    monkeypatch.setenv("GOODMEMORY_BRIDGE_TOKEN", "test-token")

    provider = plugin.GoodMemoryMemoryProvider()
    provider.initialize(
        session_id="desktop-session",
        hermes_home=str(tmp_path),
        platform="desktop",
        agent_identity="coder",
    )

    assert FakeClient.instances[-1].scope.workspace_id.startswith(
        "hermes:desktop-project:"
    )


def test_prefetch_injects_context_without_writing(plugin, tmp_path, monkeypatch):
    provider, client = initialize_provider(plugin, tmp_path, monkeypatch)

    context = provider.prefetch(
        "How do we deploy this project?", session_id="session-b"
    )
    provider.sync_turn(
        "The user said something transient.",
        "The assistant answered.",
        session_id="session-b",
    )

    assert context == "Project deploys from the release tag."
    assert client.calls == [
        (
            "recall_context",
            "How do we deploy this project?",
            {
                "retrieval_profile": "general_chat",
                "output": "system_prompt_fragment",
                "max_tokens": 1200,
            },
        )
    ]


def test_explicit_tools_preserve_provenance_and_return_auditable_results(
    plugin, tmp_path, monkeypatch
):
    provider, client = initialize_provider(plugin, tmp_path, monkeypatch)

    recall = json.loads(
        provider.handle_tool_call(
            "goodmemory_recall", {"query": "What is the deployment decision?"}
        )
    )
    remembered = json.loads(
        provider.handle_tool_call(
            "goodmemory_remember",
            {"content": "Deploy only from signed release tags.", "role": "user"},
        )
    )
    revised = json.loads(
        provider.handle_tool_call(
            "goodmemory_revise",
            {
                "memory_id": "m-1",
                "content": "Deploy only from verified release tags.",
                "reason": "User corrected signed to verified.",
            },
        )
    )
    forgotten = json.loads(
        provider.handle_tool_call("goodmemory_forget", {"memory_id": "m-2"})
    )

    assert recall == {
        "ok": True,
        "context": "Project deploys from the release tag.",
        "has_context": True,
        "item_count": 1,
        "items": FakeRecallResult.items,
        "routing": {
            "requested_strategy": "hybrid",
            "resolved_strategy": "hybrid",
            "llm_refinement": False,
            "semantic_tie_breaking": True,
            "fallback_reason": None,
            "provider_fallback": None,
        },
        "contract_version": "phase-39.http-memory.v1",
        "trace_id": "trace-1",
    }
    assert remembered["ok"] is True
    assert revised["ok"] is True
    assert forgotten["ok"] is True
    assert (
        "remember",
        [{"role": "user", "content": "Deploy only from signed release tags."}],
        {
            "annotations": [
                {
                    "messageIndex": 0,
                    "remember": "always",
                    "confirmed": True,
                    "reason": "explicit Hermes goodmemory_remember tool call",
                }
            ]
        },
    ) in client.calls
    assert any(
        call[0] == "revise" and call[1]["memory_id"] == "m-1" for call in client.calls
    )
    assert ("forget", "m-2", {}) in client.calls


def test_invalid_tool_inputs_are_rejected_before_network_calls(
    plugin, tmp_path, monkeypatch
):
    provider, client = initialize_provider(plugin, tmp_path, monkeypatch)

    missing_query = json.loads(provider.handle_tool_call("goodmemory_recall", {}))
    invalid_role = json.loads(
        provider.handle_tool_call(
            "goodmemory_remember", {"content": "secret", "role": "system"}
        )
    )
    unknown = json.loads(provider.handle_tool_call("goodmemory_unknown", {}))

    assert missing_query["ok"] is False
    assert missing_query["error"]["code"] == "query_required"
    assert invalid_role["error"]["code"] == "invalid_role"
    assert unknown["error"]["code"] == "unknown_tool"
    assert client.calls == []


def test_save_config_uses_profile_scoped_file_and_never_writes_token(plugin, tmp_path):
    provider = plugin.GoodMemoryMemoryProvider()
    provider.save_config(
        {
            "base_url": "https://memory.example.test",
            "token": "must-not-be-written",
        },
        str(tmp_path),
    )

    config_path = tmp_path / "goodmemory.json"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == {"base_url": "https://memory.example.test"}
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_system_prompt_states_write_and_remote_data_boundaries(plugin):
    block = plugin.GoodMemoryMemoryProvider().system_prompt_block()

    assert "does not automatically upload completed turns" in block
    assert "non-sensitive" in block
    assert "configured bridge" in block


def test_current_hermes_discovers_and_routes_the_standalone_plugin(
    plugin, tmp_path, monkeypatch
):
    plugin_dir = tmp_path / "plugins" / "goodmemory"
    plugin_dir.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(PLUGIN_ROOT / name, plugin_dir / name)

    from plugins import memory as memory_plugins
    from agent.memory_manager import MemoryManager

    monkeypatch.setattr(
        memory_plugins, "_get_user_plugins_dir", lambda: plugin_dir.parent
    )
    sys.modules.pop("_hermes_user_memory.goodmemory", None)
    provider = memory_plugins.load_memory_provider("goodmemory")

    assert provider is not None
    assert provider.is_available() is True

    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all(
        session_id="integration-session",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_identity="coder",
        cwd=str(tmp_path / "integration-project"),
    )

    assert manager.get_all_tool_names() == {
        "goodmemory_recall",
        "goodmemory_remember",
        "goodmemory_revise",
        "goodmemory_forget",
    }
    result = json.loads(
        manager.handle_tool_call(
            "goodmemory_remember",
            {"content": "The release branch is protected.", "role": "user"},
        )
    )
    assert result["ok"] is True
