"""Standalone reproducer for OpenMinion SDK bugs discovered while building OpenMinion Veritas.

1. Issue #703 / PR #704:
   `subagent(parent, tools=[...])` crashed with `TypeError: sequence item 0: expected str instance, function found`
   inside `SubagentRunContext.as_inbound_metadata()` when passed `@tool`-decorated functions.
2. Finding #2:
   `Agent(tools=[dup_tool, dup_tool])` deduplicated `self.tools` names in `Agent.__init__`, but appended
   `family_builder` to `self._tool_family_builders` unconditionally, raising
   `ToolRuntimeError: Tool already registered: <name>` on `.run(...)`.
"""

from __future__ import annotations

from typing import Any

from openminion import Agent, subagent, tool
from openminion.modules.tool.registry import ToolRegistry


class _MinimalRuntime:
    def __init__(self) -> None:
        self.tools = ToolRegistry()
        self.last_payload: dict[str, Any] | None = None

    def run_turn(self, *, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.last_payload = payload
        return {"body": "verified", "request_id": "repro-703"}

    def close(self) -> None:
        pass


@tool(description="Echo a diagnostic marker.")
def echo_marker(message: str) -> str:
    return f"echo:{message}"


def verify_issue_703_subagent_decorated_tool() -> dict[str, Any]:
    """Verify `subagent(parent, tools=[echo_marker])` resolves `@tool` callables to string names."""
    runtime = _MinimalRuntime()
    parent = Agent(runtime=runtime, name="parent_agent")
    child = subagent(parent, name="child_agent", tools=[echo_marker])
    result = child.run("ping")
    assert child.subagent_context is not None
    inbound_meta = child.subagent_context.as_inbound_metadata()
    return {
        "issue": "https://github.com/openminion/openminion/issues/703",
        "fix_pr": "https://github.com/openminion/openminion/pull/704",
        "child_tools": child.tools,
        "context_tool_allowlist": child.subagent_context.tool_allowlist,
        "inbound_metadata_allowlist": inbound_meta.get("subagent_tool_allowlist"),
        "reply": result.text,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(verify_issue_703_subagent_decorated_tool(), indent=2))
