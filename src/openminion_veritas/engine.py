"""Core OpenMinion Veritas orchestration engine exercising OpenMinion 0.1 SDK building blocks."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openminion import (
    APIRuntime,
    Agent,
    AgentOutputValidationError,
    Handoff,
    MemoryBundle,
    OpenMinionConfig,
    subagent,
)
from openminion.api.handoff import DelegatedMemoryReadRequest
from openminion.base.config import AgentProfileConfig
from openminion.modules.memory.portability import (
    export_bundle,
    import_bundle,
    load_bundle,
    save_bundle,
)
from sophiagraph.models import MemoryNamespace

from openminion_veritas.local_provider import VeritasLocalModelServer
from openminion_veritas.models import (
    ApprovalAuditRecord,
    PatchVerificationResult,
    QAVerdictReport,
    SecuritySubagentFinding,
)
from openminion_veritas.tools import (
    apply_surgical_patch,
    inspect_source_ast,
    run_pytest_slice,
    scan_security_surface,
)

BUGGY_BILLING_SOURCE = '''"""Sample billing module with an intentional discount defect for QA triage."""

from __future__ import annotations


def apply_discount(subtotal: float, discount_rate: float) -> float:
    """Apply a percentage discount rate (0.0 to 1.0) to subtotal."""
    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    if not (0.0 <= discount_rate <= 1.0):
        raise ValueError("discount_rate must be between 0.0 and 1.0")
    try:
        discount_amount = subtotal * discount_rate
        discounted = subtotal + discount_amount
        return round(discounted, 2)
    except Exception:
        return subtotal
'''

BUGGY_BILLING_TESTS = '''"""Regression test suite for billing.apply_discount."""

from billing import apply_discount


def test_zero_discount() -> None:
    assert apply_discount(100.0, 0.0) == 100.0


def test_twenty_percent_discount() -> None:
    assert apply_discount(100.0, 0.20) == 80.0


def test_full_discount_floor() -> None:
    assert apply_discount(50.0, 1.0) == 0.0
'''


def create_sample_buggy_workspace(root: Path) -> tuple[Path, Path]:
    """Create a realistic target Python package with a failing pytest test slice."""
    workspace = root / "target_repo"
    workspace.mkdir(parents=True, exist_ok=True)
    billing_py = workspace / "billing.py"
    test_billing_py = workspace / "test_billing.py"
    billing_py.write_text(BUGGY_BILLING_SOURCE, encoding="utf-8")
    test_billing_py.write_text(BUGGY_BILLING_TESTS, encoding="utf-8")
    return workspace, billing_py


@dataclass
class VeritasExecutionArtifact:
    """Comprehensive execution artifact returned by a VeritasOrchestrator run."""

    qa_report: QAVerdictReport
    subagent_finding: SecuritySubagentFinding
    handoff_result: PatchVerificationResult
    approval_audit: list[ApprovalAuditRecord] = field(default_factory=list)
    streamed_followup_text: str = ""
    stream_chunk_count: int = 0
    memory_bundle_summary: dict[str, Any] = field(default_factory=dict)
    runtime_diagnostics: dict[str, Any] = field(default_factory=dict)
    pre_patch_pytest: dict[str, object] = field(default_factory=dict)
    post_patch_pytest: dict[str, object] = field(default_factory=dict)


def build_veritas_config(
    *,
    base_url: str,
    api_key: str = "veritas-local-key",
    model: str = "gpt-4o-mini",
    workspace_root: Path,
) -> OpenMinionConfig:
    """Build an OpenMinionConfig wired for embedded SDK execution."""
    config = OpenMinionConfig()
    config.runtime.tool_workspace_root = str(workspace_root)
    config.providers.openai.enabled = True
    config.providers.openai.api_key = api_key
    config.providers.openai.base_url = base_url
    config.providers.openai.default_model = model
    config.providers.openai.models = [model]

    for agent_name in ("veritas_lead", "security_subagent", "patch_reviewer"):
        config.agents[agent_name] = AgentProfileConfig(
            name=agent_name,
            provider="openai",
            default_channel="console",
        )
    config.default_agent = "veritas_lead"
    return config


class VeritasOrchestrator:
    """Autonomous Repository QA, Regression Triage & Patch Verification Orchestrator."""

    def __init__(
        self,
        *,
        provider_mode: str = "local",
        approve_dangerous: bool = True,
    ) -> None:
        self.provider_mode = provider_mode
        self.approve_dangerous = approve_dangerous

    def run_workflow(self, workspace_root: Path | None = None) -> VeritasExecutionArtifact:
        """Execute the full multi-stage OpenMinion Veritas QA & Patch Verification workflow."""
        if workspace_root is None:
            with tempfile.TemporaryDirectory(prefix="openminion-veritas-") as tmp:
                return self._execute_in_workspace(Path(tmp))
        return self._execute_in_workspace(workspace_root)

    def _resolve_provider_endpoint(
        self, local_server: VeritasLocalModelServer | None
    ) -> tuple[str, str, str]:
        if self.provider_mode == "cortensor":
            base_url = os.environ.get("CORTENSOR_BASE_URL", "http://127.0.0.1:5010/v1")
            api_key = os.environ.get("CORTENSOR_API_KEY", "cortensor-dev-key")
            model = os.environ.get("CORTENSOR_MODEL", "cortensor-default")
            return base_url, api_key, model
        if self.provider_mode == "openai":
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            api_key = os.environ.get("OPENAI_API_KEY", "")
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            return base_url, api_key, model
        if local_server is None:
            raise RuntimeError("local_server is required when provider_mode='local'")
        return local_server.base_url, "veritas-local-key", "gpt-4o-mini"

    def _execute_in_workspace(self, root: Path) -> VeritasExecutionArtifact:
        target_dir, billing_py = create_sample_buggy_workspace(root)
        home_root = root / "om_home"
        data_root = root / "om_data"
        home_root.mkdir(parents=True, exist_ok=True)
        data_root.mkdir(parents=True, exist_ok=True)

        # Baseline pytest before agent intervention
        pre_patch_pytest = run_pytest_slice(str(target_dir), "test_billing.py")

        local_server: VeritasLocalModelServer | None = None
        if self.provider_mode == "local":
            local_server = VeritasLocalModelServer(
                target_file=str(billing_py),
                target_dir=str(target_dir),
            )
            local_server.start()

        try:
            base_url, api_key, model = self._resolve_provider_endpoint(local_server)
            config = build_veritas_config(
                base_url=base_url,
                api_key=api_key,
                model=model,
                workspace_root=target_dir,
            )
            runtime = APIRuntime.from_config(
                config=config,
                home_root=home_root,
                data_root=data_root,
            )
            try:
                approval_audit: list[ApprovalAuditRecord] = []

                def operator_approval_gate(
                    tool_name: str,
                    args: dict[str, Any],
                    approval_id: str,
                ) -> bool:
                    args_dict = dict(args) if isinstance(args, dict) else {}
                    if tool_name == "apply_surgical_patch":
                        allowed = self.approve_dangerous
                    else:
                        allowed = True
                    approval_audit.append(
                        ApprovalAuditRecord(
                            approval_id=str(approval_id),
                            tool_name=str(tool_name),
                            arguments=args_dict,
                            decision="APPROVED" if allowed else "DENIED",
                        )
                    )
                    return allowed

                session_id = "veritas-qa-session-001"
                thread_id = "veritas-qa-thread-001"

                # Stage 1: Lead QA & Patch Agent with @tool, dangerous tool gating, and typed output
                lead_agent = Agent(
                    runtime=runtime,
                    name="veritas_lead",
                    instructions=(
                        "You are OpenMinion Veritas, an autonomous Python QA & patch engineer. "
                        "Inspect AST defects, apply surgical fixes under operator policy approval, "
                        "and verify pytest slices."
                    ),
                    tools=[inspect_source_ast, apply_surgical_patch, run_pytest_slice],
                    output_type=QAVerdictReport,
                )

                # Stage 2: Bounded Security Subagent with isolated context, @tool allowlist (PR #704),
                # and scoped DelegatedMemoryReadRequest
                security_child = subagent(
                    lead_agent,
                    name="security_subagent",
                    instructions=(
                        "You are security_subagent. Scan target Python modules for hardcoded "
                        "credentials or unsafe eval/exec calls inside an isolated context."
                    ),
                    tools=[scan_security_surface],
                    output_type=SecuritySubagentFinding,
                    memory=DelegatedMemoryReadRequest(
                        namespaces=(MemoryNamespace(agent_id="veritas_lead"),),
                        record_types=("fact",),
                        max_results=3,
                        max_context_tokens=256,
                    ),
                )
                subagent_run = security_child.run(
                    f"Scan security surface for {billing_py}",
                    session_id="subagent-isolated-session",
                )
                subagent_finding = subagent_run.output

                # Stage 3: Execute Lead Agent QA + Surgical Patch + Pytest verification turn
                try:
                    lead_run = lead_agent.run(
                        f"Inspect AST of {billing_py}, apply_patch to fix discount bug, and verify tests.",
                        session_id=session_id,
                        thread_id=thread_id,
                        approval_callback=operator_approval_gate,
                    )
                    qa_report = lead_run.output
                except AgentOutputValidationError as exc:
                    if "confirmation denied" not in exc.raw_text.lower():
                        raise
                    qa_report = QAVerdictReport(
                        verdict="NEEDS_REVIEW",
                        target_module=str(billing_py),
                        root_cause="Operator denied dangerous patch execution via approval_callback.",
                        defects_found=[
                            "line 14: suspicious addition in discount calculation (subtotal + discount_amount)"
                        ],
                        patch_applied=False,
                        tests_passed=int(pre_patch_pytest.get("passed_count", 1)),
                        subagent_summary=subagent_finding.finding_summary,
                        handoff_verdict="PENDING_APPROVAL",
                        recommendations=[exc.raw_text],
                    )

                # Stage 4: Peer Agent Handoff (Coordinator -> patch_reviewer)
                reviewer_agent = Agent(
                    runtime=runtime,
                    name="patch_reviewer",
                    instructions=(
                        "You are patch_reviewer. Independently review the regression test evidence "
                        "and return a PatchVerificationResult."
                    ),
                )
                handoff_coordinator = Agent(
                    runtime=runtime,
                    name="veritas_lead",
                    instructions="Delegate regression verification to patch_reviewer via handoff.",
                    handoffs=[
                        Handoff(
                            target=reviewer_agent,
                            description="Transfer to patch_reviewer for independent regression sign-off.",
                        )
                    ],
                    output_type=PatchVerificationResult,
                )
                handoff_run = handoff_coordinator.run(
                    f"handoff_verify regression suite in {target_dir}",
                    session_id="veritas-handoff-session",
                    approval_callback=operator_approval_gate,
                )
                handoff_result = handoff_run.output

                # Stage 5: Multi-turn Session Continuity + SSE Streaming (`run_stream`)
                followup_agent = Agent(
                    runtime=runtime,
                    name="veritas_lead",
                    instructions="Provide a concise streaming executive summary of the session.",
                )
                deltas: list[str] = []
                stream_res = followup_agent.run_stream(
                    "Provide a follow-up executive summary for this session.",
                    session_id=session_id,
                    thread_id=thread_id,
                    on_delta=lambda chunk: deltas.append(chunk),
                )

                # Stage 6: Memory Portability (`MemoryBundle` export, save, load, import)
                bundle = export_bundle(
                    items=[
                        {
                            "id": "mem-qa-verdict-001",
                            "kind": "qa_verdict",
                            "content": qa_report.model_dump(),
                        },
                        {
                            "id": "mem-sec-finding-001",
                            "kind": "security_subagent_finding",
                            "content": subagent_finding.model_dump(),
                        },
                        {
                            "id": "mem-approvals-001",
                            "kind": "operator_approval_audit",
                            "content": [rec.model_dump() for rec in approval_audit],
                        },
                    ],
                    metadata={
                        "agent": "openminion-veritas",
                        "session_id": session_id,
                        "target_module": str(billing_py),
                    },
                )
                bundle_path = root / "veritas_memory_bundle.json"
                save_bundle(bundle, bundle_path)
                reloaded_bundle: MemoryBundle = load_bundle(bundle_path)
                direct_import = import_bundle(reloaded_bundle, trust_mode="direct")
                candidate_import = import_bundle(reloaded_bundle, trust_mode="candidate")

                # Stage 7: Runtime Diagnostics & Self-Model Queries
                tool_inventory = runtime.tool_inventory_report(readiness=True)
                capability_rep = runtime.capability_report(agent_id="veritas_lead")
                posture_rep = runtime.runtime_posture(agent_id="veritas_lead")
                self_model_rep = runtime.runtime_self_model(agent_id="veritas_lead")

                post_patch_pytest = run_pytest_slice(str(target_dir), "test_billing.py")

                return VeritasExecutionArtifact(
                    qa_report=qa_report,
                    subagent_finding=subagent_finding,
                    handoff_result=handoff_result,
                    approval_audit=approval_audit,
                    streamed_followup_text=stream_res.text,
                    stream_chunk_count=len(deltas),
                    memory_bundle_summary={
                        "bundle_version": reloaded_bundle.version,
                        "item_count": len(reloaded_bundle.items),
                        "direct_import": direct_import,
                        "candidate_import": candidate_import,
                    },
                    runtime_diagnostics={
                        "registered_tool_count": len(tool_inventory),
                        "canonical_turn_path": list(posture_rep.get("canonical_turn_path", ())),
                        "capability_agent_id": capability_rep.get("agent_id", "veritas_lead"),
                        "self_model_present": bool(self_model_rep),
                    },
                    pre_patch_pytest=pre_patch_pytest,
                    post_patch_pytest=post_patch_pytest,
                )
            finally:
                runtime.close()
        finally:
            if local_server is not None:
                local_server.close()
