# What We Learned Building on OpenMinion 0.1 SDK

While building **OpenMinion Veritas** against `openminion` (`0.1.19` -> `0.1.20`), we stress-tested the embedded Python SDK across agent execution, custom `@tool` registration, `PolicyCtl` operator approval gates, `subagent` delegation, `DelegatedMemoryReadRequest`, `Handoff`, `MemoryBundle`, and `APIRuntime` diagnostics.

Below are our **technical findings & merged upstream contributions** (including **upstream bug report [#703](https://github.com/openminion/openminion/issues/703) + 2 MERGED upstream fix PRs [#704](https://github.com/openminion/openminion/pull/704) & [#706](https://github.com/openminion/openminion/pull/706) shipped in `openminion==0.1.20` on PyPI**, **2 additional reproducible SDK bugs**, and **2 developer-experience friction points**) with exact file/line references and minimal reproducers.

---

## Finding #1 (Bug Report [#703](https://github.com/openminion/openminion/issues/703) & MERGED Fix PRs [#704](https://github.com/openminion/openminion/pull/704) + [#706](https://github.com/openminion/openminion/pull/706)): `subagent(parent, tools=[...])` `@tool` allowlist crash & `BrainRunner` memory-write guard

- **Upstream Issue**: [`openminion/openminion#703`](https://github.com/openminion/openminion/issues/703)
- **Upstream Fix PR #1**: [`openminion/openminion#704`](https://github.com/openminion/openminion/pull/704) (Reviewed, Approved & **MERGED** by `@wireindevice`, shipped in `openminion==0.1.20`)
- **Upstream Fix PR #2**: [`openminion/openminion#706`](https://github.com/openminion/openminion/pull/706) (`fix(brain): read _memory_write_allowed_for_turn directly (Refs #647)`, **MERGED** & shipped in `openminion==0.1.20`)
- **Affected File**: `src/openminion/api/handoff.py:244-314` (`subagent`) and `src/openminion/api/handoff.py:92-104` (`SubagentRunContext.as_inbound_metadata`)

### Root Cause
`subagent()` accepts `tools: list[ToolInput] | None = None`, where `ToolInput = str | Callable[..., Any]`.
However, `subagent()` constructed `SubagentRunContext(..., tool_allowlist=tuple(tools or ()))` **before** `Agent.__init__` normalized `@tool`-decorated functions into string tool names (`child.tools`).
When `child.run(...)` subsequently serialized `SubagentRunContext.as_inbound_metadata()`, `",".join(self.tool_allowlist)` raised:
```
TypeError: sequence item 0: expected str instance, function found
```

### Standalone Reproducer (`python repro_issue_703.py`)
```python
from openminion import Agent, subagent, tool

@tool
def ping(msg: str) -> str:
    return f"pong: {msg}"

parent = Agent(name="parent")
child = subagent(parent, name="child", tools=[ping])
child.run("hello")  # Raised TypeError before PR #704
```

### Fix Delivered in PR #704
Construct `child = Agent(...)` first so `Agent.__init__` validates `tools` and populates `child.tools` with resolved `str` tool names, then bind `child.subagent_context = SubagentRunContext(..., tool_allowlist=tuple(child.tools))`.

---

## Finding #2 (Reproducible SDK Bug): `Agent(tools=[dup_tool, dup_tool])` deduplicates `self.tools` but duplicates `self._tool_family_builders`, crashing at runtime

- **Affected File**: `src/openminion/api/agent.py:111-113` (`Agent.__init__`) and `src/openminion/api/agent.py:217-227` (`Agent._register_tools_for_run`)

### Root Cause
In `Agent.__init__` (`src/openminion/api/agent.py:111-113`):
```python
self._tool_family_builders.append(family_builder)
if tool_name not in self.tools:
    self.tools.append(tool_name)
```
`self.tools` checks `if tool_name not in self.tools:`, indicating duplicate tool entries in `tools=[...]` were meant to be deduplicated idempotently. Because `self._tool_family_builders.append(family_builder)` is outside the `if` block, passing the same `@tool` function twice (e.g. when composing tool bundles from multiple helpers) appends two identical `family_builder` callables. On `.run(...)`, `_register_tools_for_run` calls `registry.add(spec)` twice for the same tool name and crashes with:
```
openminion.modules.tool.errors.ToolRuntimeError: Tool already registered: <name>
```

### Minimal Fix Recommendation
Move `self._tool_family_builders.append(family_builder)` inside `if tool_name not in self.tools:`.

---

## Finding #3 (Reproducible SDK Bug): `Handoff(target=...)` fails with `Tool execution failed` when the target `Agent` sets `output_type=BaseModel` or registers temporary `@tool` functions on a shared `APIRuntime`

- **Affected File**: `src/openminion/api/handoff.py:162-176` (`build_delegate_tool`)

### Root Cause
In `build_delegate_tool` (`src/openminion/api/handoff.py:168-175`):
```python
def _handler_fn(message: str) -> dict[str, Any]:
    result = handoff.target.run(message)
    data = {"output": result.output}
    ...
    return {"ok": True, "content": result.text, "data": data}
```
1. **Unserialized `BaseModel` in `data["output"]`**: When `handoff.target` specifies `output_type=SomePydanticModel`, `result.output` is a `BaseModel` instance rather than a JSON-serializable `dict` or `str`. When the parent agent's tool executor serializes the `transfer_to_<name>` tool result, `BaseModel` in `data["output"]` is not converted via `.model_dump(mode="json")`.
2. **Nested temporary tool registration on shared `APIRuntime`**: When `handoff.target` is executed synchronously inside the parent agent's `transfer_to_<name>` tool handler while sharing the same `APIRuntime`, registering temporary `@tool` functions on `handoff.target` collides with the active parent turn execution and raises `RuntimeError` (`EXEC_ERROR: Tool execution failed`).

### Minimal Fix Recommendation
In `build_delegate_tool`:
```python
output_val = result.output.model_dump(mode="json") if isinstance(result.output, BaseModel) else result.output
```
And allow `Agent._register_tools_for_run` to be re-entrant across parent-to-handoff turns when a tool name is not yet registered.

---

## Finding #4 (DX & Policy Ergonomics): `@tool` defaults to `min_scope="WRITE_SAFE"` and `idempotent=False`, causing read-only tools to pause for operator confirmation

- **Affected Files**:
  - `src/openminion/tools/decorator.py:249,308`
  - `src/openminion/modules/policy/runtime/action_policy.py:154-196`

### What Happened
When decorating a read-only helper with `@tool` (without extra keyword arguments), `_decorate` sets `min_scope_default="WRITE_SAFE"` and `idempotent=False`.
In `derive_tool_risk_spec` (`action_policy.py:170-183`):
- Only `min_scope == "READ_ONLY"` maps to `POLICY_RISK_READ` (`default_confirm=False`).
- `min_scope == "WRITE_SAFE"` with `idempotent=False` maps to `POLICY_RISK_WRITE` with `default_confirm=True`.

As a result, a developer writing `@tool def get_weather(city: str)` gets `"Policy confirmation required"` on every call unless they either pass `approval_callback` to `.run(...)` or explicitly decorate with `@tool(min_scope="READ_ONLY", idempotent=True)`.

### Recommendation
Document `min_scope="READ_ONLY"` prominently in the `@tool` docstring and SDK quickstart, or expose a `@read_tool` / `read_only=True` shorthand on `@tool`.

---

## Finding #5 (DX & Lifecycle Ergonomics): `AgentOutputValidationError` on Policy Denial + `APIRuntime` Context Manager

- **Affected Files**:
  - `src/openminion/modules/brain/runner/delegates.py:324-330` & `src/openminion/api/agent.py:252-284`
  - `src/openminion/api/runtime.py:34` (`APIRuntime`)
  - `src/openminion/base/config.py` (`AgentProfileConfig.default_channel`)

### What Happened
1. **`output_type` + `approval_callback` denial**: When `approval_callback` returns `False` for a `@tool(dangerous=True)` call, `BrainRunner` immediately returns an `AskUserCommand` with text `"<agent>: Confirmation denied. The action was not run."` without giving the LLM a final turn to format a structured JSON response. If the `Agent` was configured with `output_type=MyPydanticModel`, `Agent._coerce_output` raises `AgentOutputValidationError` on that plain-text denial string.
2. **`APIRuntime` context manager**: `Agent` supports `with Agent(...) as agent:`, which closes its owned runtime on exit. However, when sharing an explicit `APIRuntime` across multiple agents (`lead_agent`, `security_subagent`, `patch_reviewer`), `APIRuntime` itself does not implement `__enter__` / `__exit__`, requiring manual `try ... finally: runtime.close()`.
3. **`AgentProfileConfig.default_channel`**: Programmatic `OpenMinionConfig()` profiles default to `default_channel=""`, which causes `GatewayService` to reject embedded SDK turns with `RuntimeError: inbound authenticity denied (reason=invalid_channel, mode=warn)` unless `default_channel="console"` is explicitly set on each `AgentProfileConfig`.

### Recommendation
1. Add `__enter__` and `__exit__` to `APIRuntime`.
2. Default `AgentProfileConfig.default_channel` to `"console"` when used via `APIRuntime.from_config`.
3. Raise a dedicated `ToolApprovalDeniedError` (or populate `AgentRunResult.run_state = "policy_denied"`) when `approval_callback` denies a tool call on a typed `output_type` agent.
