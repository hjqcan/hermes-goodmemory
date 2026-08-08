"""Opt-in live bridge proof for the GoodMemory Hermes provider."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_goodmemory_live_plugin",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if not spec or not spec.loader:
        raise RuntimeError("could not load plugin module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def invoke(provider, tool_name: str, args: dict) -> dict:
    payload = json.loads(provider.handle_tool_call(tool_name, args))
    if payload.get("ok") is not True:
        raise AssertionError(f"{tool_name} failed: {payload}")
    return payload


def main() -> None:
    base_url = os.environ.get("GOODMEMORY_LIVE_URL", "http://127.0.0.1:18739")
    if not os.environ.get("GOODMEMORY_BRIDGE_TOKEN"):
        raise SystemExit("GOODMEMORY_BRIDGE_TOKEN is required")

    project_root = Path(
        os.environ.get("GOODMEMORY_LIVE_PROJECT_ROOT", tempfile.gettempdir())
    )
    workspace_a = (project_root / "goodmemory-hermes-live-a").resolve()
    workspace_b = (project_root / "goodmemory-hermes-live-b").resolve()
    workspace_a.mkdir(parents=True, exist_ok=True)
    workspace_b.mkdir(parents=True, exist_ok=True)

    plugin = load_plugin()
    marker = f"ORION-{uuid.uuid4().hex[:10]}"
    original = (
        f"Hermes GoodMemory live proof decision: deploy {marker} only after "
        "blue-gate approval."
    )
    corrected = (
        f"Hermes GoodMemory live proof decision: deploy {marker} only after "
        "green-gate approval."
    )

    with tempfile.TemporaryDirectory(prefix="hermes-goodmemory-home-") as home:
        config_writer = plugin.GoodMemoryMemoryProvider()
        config_writer.save_config({"base_url": base_url}, home)

        first = plugin.GoodMemoryMemoryProvider()
        first.initialize(
            session_id="live-write",
            hermes_home=home,
            platform="cli",
            agent_identity="live-proof",
            cwd=str(workspace_a),
        )
        write = invoke(
            first,
            "goodmemory_remember",
            {"content": original, "role": "user"},
        )

        second = plugin.GoodMemoryMemoryProvider()
        second.initialize(
            session_id="live-recall-new-session",
            hermes_home=home,
            platform="cli",
            agent_identity="live-proof",
            cwd=str(workspace_a),
        )
        same_workspace = invoke(
            second,
            "goodmemory_recall",
            {"query": f"What approval gate controls deployment of {marker}?"},
        )
        matching = [
            item
            for item in same_workspace.get("items", [])
            if marker in str(item.get("content", ""))
        ]
        if not matching:
            raise AssertionError(
                f"new session did not recall {marker}; write={write}; "
                f"recall={same_workspace}"
            )
        memory_id = str(matching[0].get("memoryId", ""))
        if not memory_id:
            raise AssertionError("recalled item did not expose memoryId")

        isolated = plugin.GoodMemoryMemoryProvider()
        isolated.initialize(
            session_id="live-other-workspace",
            hermes_home=home,
            platform="cli",
            agent_identity="live-proof",
            cwd=str(workspace_b),
        )
        other_workspace = invoke(
            isolated,
            "goodmemory_recall",
            {"query": f"What approval gate controls deployment of {marker}?"},
        )
        if marker in json.dumps(other_workspace):
            raise AssertionError("memory crossed the derived workspace boundary")

        revision = invoke(
            second,
            "goodmemory_revise",
            {
                "memory_id": memory_id,
                "content": corrected,
                "reason": "Live proof correction from blue-gate to green-gate.",
            },
        )
        after_revision = invoke(
            second,
            "goodmemory_recall",
            {"query": f"What approval gate controls deployment of {marker}?"},
        )
        if corrected not in json.dumps(after_revision):
            raise AssertionError(f"revision was not recalled: {after_revision}")
        revised_items = [
            item
            for item in after_revision.get("items", [])
            if corrected == str(item.get("content", ""))
        ]
        if not revised_items or not revised_items[0].get("memoryId"):
            raise AssertionError(
                f"revised item did not expose its current memoryId: {after_revision}"
            )
        current_memory_id = str(revised_items[0]["memoryId"])

        deletion = invoke(
            second,
            "goodmemory_forget",
            {"memory_id": current_memory_id},
        )
        after_delete = invoke(
            second,
            "goodmemory_recall",
            {"query": f"What approval gate controls deployment of {marker}?"},
        )
        if marker in json.dumps(after_delete):
            raise AssertionError(f"deleted memory was still recalled: {after_delete}")

        print(
            json.dumps(
                {
                    "ok": True,
                    "contract_version": same_workspace["contract_version"],
                    "marker": marker,
                    "write_operation": write.get("operation"),
                    "recalled_in_new_session": True,
                    "workspace_isolation": True,
                    "routing": same_workspace["routing"],
                    "revision_operation": revision.get("operation"),
                    "deletion_operation": deletion.get("operation"),
                    "deleted_memory_absent": True,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
