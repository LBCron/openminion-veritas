# OpenMinion 0.1 SDK — Building Blocks Tested

This document details every **OpenMinion 0.1 Python SDK (`0.1.19` / `dev`)** primitive exercised by **OpenMinion Veritas**, how it is wired in code, what worked smoothly, and what broke or required workarounds.

---

## 1. Summary Matrix of OpenMinion Building Blocks

| # | OpenMinion Building Block | SDK Symbols / Modules Used | Source Location in `openminion-veritas` | Status |
|---|---|---|---|---|
| 1 | **Runtime & Config Bootstrap** | `OpenMinionConfig`, `AgentProfileConfig`, `APIRuntime.from_config` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (`build_veritas_config`, `VeritasOrchestrator._execute_in_workspace`) | Verified |
| 2 | **Agent Execution & Typed Outputs** | `Agent`, `AgentRunResult`, `AgentOutputValidationError`, `output_type=BaseModel` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py), [`src/openminion_veritas/models.py`](src/openminion_veritas/models.py) | Verified |
| 3 | **Custom Tools & Risk Scopes** | `@tool(min_scope="READ_ONLY", idempotent=True)`, `@tool(dangerous=True)` | [`src/openminion_veritas/tools.py`](src/openminion_veritas/tools.py) (`inspect_source_ast`, `run_pytest_slice`, `scan_security_surface`, `apply_surgical_patch`) | Verified |
| 4 | **Permissions & Operator Policy Gate** | `PolicyCtl`, `temporary_tool_risks`, `Agent.run(..., approval_callback=...)` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (`operator_approval_gate`) | Verified (Allow + Deny paths tested) |
| 5 | **Bounded Subagent Delegation** | `subagent(parent, name=..., tools=[...], output_type=..., memory=...)`, `SubagentRunContext` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py), [`repro_issue_703.py`](repro_issue_703.py) | Found & fixed SDK Bug [#703](https://github.com/openminion/openminion/issues/703) in PR [#704](https://github.com/openminion/openminion/pull/704) |
| 6 | **Delegated Scoped Memory Read** | `DelegatedMemoryReadRequest`, `MemoryNamespace` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (Stage 2 `security_child`) | Verified |
| 7 | **Peer Agent Handoffs** | `Handoff(target=..., description=...)`, `transfer_to_<name>` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (Stage 4 `handoff_coordinator -> patch_reviewer`) | Verified (with nested-runtime & output_type findings documented) |
| 8 | **Sessions, Threads & Streaming** | `session_id`, `thread_id`, `Agent.run_stream(..., on_delta=...)` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (Stages 3 & 5) | Verified |
| 9 | **Portable Memory Bundles** | `MemoryBundle`, `export_bundle`, `save_bundle`, `load_bundle`, `import_bundle` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (Stage 6) | Verified (`direct` + `candidate` trust modes) |
| 10 | **Runtime Diagnostics & Self-Model** | `tool_inventory_report`, `capability_report`, `runtime_posture`, `runtime_self_model` | [`src/openminion_veritas/engine.py`](src/openminion_veritas/engine.py) (Stage 7) | Verified |

---

## 2. Detailed Walkthrough of Each Building Block

### 2.1 `OpenMinionConfig` & `APIRuntime.from_config`
- **What we tested**: Programmatic construction of `OpenMinionConfig` with `AgentProfileConfig` entries (`veritas_lead`, `security_subagent`, `patch_reviewer`) and embedded initialization via `APIRuntime.from_config(config=config, home_root=home_root, data_root=data_root)`.
- **What worked**: SQLite-backed session, retrieve, policy, and bitemporal memory migrations bootstrap automatically inside `data_root` and `home_root` with zero external database dependencies.
- **What surprised us**:
  1. `AgentProfileConfig` must explicitly set `default_channel="console"` when running embedded SDK turns; leaving `default_channel` at its empty default causes `GatewayService` to reject the turn with `RuntimeError: inbound authenticity denied (reason=invalid_channel, mode=warn)`.
  2. Unlike `Agent` (which implements `__enter__`/`__exit__`), `APIRuntime` does not implement the context-manager protocol, requiring an explicit `try ... finally: runtime.close()` block.

### 2.2 Custom `@tool` Decorator & `PolicyCtl` Risk Derivation
- **What we tested**:
  - Three read-only diagnostic tools (`inspect_source_ast`, `run_pytest_slice`, `scan_security_surface`) decorated with `@tool(min_scope="READ_ONLY", idempotent=True)`.
  - One file-mutating remediation tool (`apply_surgical_patch`) decorated with `@tool(dangerous=True)`.
- **What worked**:
  - `Agent._register_tools_for_run` automatically synthesizes a `ToolExposureProfile` (`user_tool_<name>`) with `default_active=True` so custom `@tool` functions are immediately visible to the model and unregistered cleanly when the turn exits.
  - `@tool(dangerous=True)` maps to `POLICY_RISK_DESTRUCTIVE` (`default_confirm=True`), triggering `approval_callback(tool_name, args, approval_id)` before execution.
- **What surprised us**:
  - `@tool` sets `min_scope_default="WRITE_SAFE"` and `idempotent=False` by default (`openminion/tools/decorator.py:249,308`). Furthermore, `derive_tool_risk_spec` (`openminion/modules/policy/runtime/action_policy.py:170`) checks `if min_scope == "READ_ONLY":` to classify a tool as `POLICY_RISK_READ`. Consequently, any `@tool` that omits `min_scope="READ_ONLY"` (or `idempotent=True`) is treated as an unconfirmed write tool and pauses the turn with `"Policy confirmation required"` unless an `approval_callback` is supplied.

### 2.3 Bounded Subagent Delegation (`subagent`) & Scoped Memory (`DelegatedMemoryReadRequest`)
- **What we tested**:
  - Spawning a child agent via `subagent(lead_agent, name="security_subagent", tools=[scan_security_surface], output_type=SecuritySubagentFinding, memory=DelegatedMemoryReadRequest(namespaces=(MemoryNamespace(agent_id="veritas_lead"),), record_types=("fact",), max_results=3, max_context_tokens=256))`.
- **What broke & how we fixed it upstream**:
  - Passing `@tool`-decorated functions in `subagent(parent, tools=[scan_security_surface])` crashed with `TypeError: sequence item 0: expected str instance, function found` inside `SubagentRunContext.as_inbound_metadata()`.
  - We traced the root cause to `src/openminion/api/handoff.py:281-303`, filed **[Issue #703](https://github.com/openminion/openminion/issues/703)**, and submitted **[PR #704](https://github.com/openminion/openminion/pull/704)** (reviewed and approved by `@wireindevice`).
- **What worked**:
  - Once fixed, `subagent` enforces single-hop depth (`max_depth=1`), fresh isolated child session IDs, one-shot `PolicyCtl` delegated memory grants that are automatically revoked in `finally:`, and strict tool allowlisting.

### 2.4 Peer Agent Delegation (`Handoff`)
- **What we tested**:
  - Compiling a peer `Agent(name="patch_reviewer")` into a `transfer_to_patch_reviewer` tool via `Handoff(target=reviewer_agent)` and executing a full coordinator-to-reviewer handoff turn.
- **What worked**:
  - `build_delegate_tool` automatically generates the `transfer_to_<name>` tool schema with a `message: str` argument model and routes the message to `handoff.target.run(message)`.
- **What broke / edge cases discovered**:
  1. Calling a `Handoff` target that registers its own `@tool` functions on the **same** `APIRuntime` instance while the parent agent is mid-turn raises a `RuntimeError` during nested tool execution.
  2. Setting `output_type=BaseModel` on the `Handoff` target causes `build_delegate_tool` (`src/openminion/api/handoff.py:170`) to place a raw Pydantic `BaseModel` instance inside `data["output"]`, which fails JSON serialization in the parent turn's tool executor (`Object of type <Model> is not JSON serializable`). Setting `output_type=PatchVerificationResult` on the coordinator agent instead validates the handoff output cleanly.

### 2.5 Typed Output Coercion (`output_type`) & Policy Denial Interaction
- **What we tested**:
  - `output_type=QAVerdictReport`, `output_type=SecuritySubagentFinding`, and `output_type=PatchVerificationResult` across both approved and operator-denied (`approve_dangerous=False`) runs.
- **What worked**:
  - Pydantic v2 `model_validate_json` coercion works cleanly and wraps malformed replies in `AgentOutputValidationError` with `.raw_text` and `.validation_error` preserved.
- **What surprised us**:
  - When `approval_callback` returns `False` (`DENY`), `BrainRunner` short-circuits the turn with an `AskUserCommand` (`"veritas_lead: Confirmation denied. The action was not run."`) without invoking the model a final time. If the `Agent` also declares `output_type=BaseModel`, `Agent._coerce_output` raises `AgentOutputValidationError` on that plain-text denial message. Catching `AgentOutputValidationError` and inspecting `exc.raw_text` allows applications to handle operator denials gracefully.

### 2.6 Sessions, Streaming (`run_stream`), Memory Portability (`MemoryBundle`), and Diagnostics
- **What we tested**:
  - Multi-turn continuity across Stage 3 (`lead_agent.run(..., session_id=..., thread_id=...)`) and Stage 5 (`followup_agent.run_stream(..., session_id=..., thread_id=..., on_delta=...)`).
  - Exporting structured QA artifacts into a `MemoryBundle`, persisting via `save_bundle` / `load_bundle`, and validating `import_bundle` under both `trust_mode="direct"` and `trust_mode="candidate"`.
  - Querying `runtime.tool_inventory_report(readiness=True)`, `runtime.capability_report()`, `runtime.runtime_posture()`, and `runtime.runtime_self_model()`.
- **What worked**:
  - Session state, active-state prompt composition (`ASPM-05`), `MemoryBundle` JSON serialization, and runtime self-model / posture introspection all worked reliably out of the box.
