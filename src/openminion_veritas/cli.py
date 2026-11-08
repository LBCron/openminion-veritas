"""Command-line interface for OpenMinion Veritas."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from openminion_veritas.engine import VeritasExecutionArtifact, VeritasOrchestrator


def _serialize_artifact(artifact: VeritasExecutionArtifact) -> dict[str, object]:
    return {
        "qa_report": artifact.qa_report.model_dump(),
        "subagent_finding": artifact.subagent_finding.model_dump(),
        "handoff_result": artifact.handoff_result.model_dump(),
        "approval_audit": [rec.model_dump() for rec in artifact.approval_audit],
        "streamed_followup_text": artifact.streamed_followup_text,
        "stream_chunk_count": artifact.stream_chunk_count,
        "memory_bundle_summary": artifact.memory_bundle_summary,
        "runtime_diagnostics": artifact.runtime_diagnostics,
        "pre_patch_pytest": artifact.pre_patch_pytest,
        "post_patch_pytest": artifact.post_patch_pytest,
    }


def _render_rich_output(artifact: VeritasExecutionArtifact, console: Console) -> None:
    console.print(
        Panel.fit(
            "[bold cyan]OpenMinion Veritas[/bold cyan] — Autonomous Repository QA, "
            "Regression Triage & Patch Verification Agent\n"
            "[dim]Powered by OpenMinion 0.1 Python SDK (`APIRuntime`, `Agent`, `@tool`, `subagent`, `Handoff`, `MemoryBundle`)[/dim]",
            border_style="cyan",
        )
    )

    table = Table(title="OpenMinion 0.1 SDK Building Blocks Executed", show_lines=True)
    table.add_column("Stage / Building Block", style="bold")
    table.add_column("SDK Primitive", style="cyan")
    table.add_column("Outcome", style="green")

    pre_passed = artifact.pre_patch_pytest.get("passed_count", 0)
    pre_failed = artifact.pre_patch_pytest.get("failed_count", 0)
    post_passed = artifact.post_patch_pytest.get("passed_count", 0)
    post_failed = artifact.post_patch_pytest.get("failed_count", 0)

    table.add_row(
        "1. Baseline Regression Slice",
        "@tool run_pytest_slice",
        f"Pre-patch: {pre_passed} passed, {pre_failed} failed",
    )
    table.add_row(
        "2. Bounded Security Subagent",
        "subagent(..., tools=[@tool], memory_read=DelegatedMemoryReadRequest)",
        f"Risk={artifact.subagent_finding.risk_level} | Isolated={artifact.subagent_finding.isolated_context_verified}",
    )
    table.add_row(
        "3. Lead QA AST + Surgical Patch",
        "Agent(tools=[...], output_type=QAVerdictReport, approval_callback=...)",
        f"Verdict={artifact.qa_report.verdict} | Patched={artifact.qa_report.patch_applied}",
    )
    table.add_row(
        "4. Operator Policy Gate",
        "@tool(dangerous=True) + PolicyCtl approval_callback",
        f"{len(artifact.approval_audit)} policy decisions recorded ({', '.join(f'{a.tool_name}:{a.decision}' for a in artifact.approval_audit)})",
    )
    table.add_row(
        "5. Peer Reviewer Handoff",
        "Handoff(target=patch_reviewer) -> transfer_to_patch_reviewer",
        f"Reviewer={artifact.handoff_result.reviewer_verdict} | Post-patch: {post_passed} passed, {post_failed} failed",
    )
    table.add_row(
        "6. Multi-Turn Session + Streaming",
        "Agent.run_stream(session_id=..., thread_id=..., on_delta=...)",
        f"{artifact.stream_chunk_count} SSE chunks streamed | {artifact.streamed_followup_text[:72]}...",
    )
    table.add_row(
        "7. Portable Memory Bundle",
        "MemoryBundle / export_bundle / save_bundle / load_bundle / import_bundle",
        f"v={artifact.memory_bundle_summary.get('bundle_version')} | items={artifact.memory_bundle_summary.get('item_count')}",
    )
    table.add_row(
        "8. Runtime Diagnostics & Self-Model",
        "tool_inventory_report / capability_report / runtime_posture / runtime_self_model",
        f"tools={artifact.runtime_diagnostics.get('registered_tool_count')} | self_model={artifact.runtime_diagnostics.get('self_model_present')}",
    )
    console.print(table)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openminion-veritas",
        description="OpenMinion Veritas — Autonomous Repository QA & Patch Verification Agent",
    )
    subparsers = parser.add_subparsers(dest="command")

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the end-to-end OpenMinion Veritas QA & Patch Verification workflow",
    )
    demo_parser.add_argument(
        "--provider",
        choices=("local", "cortensor", "openai"),
        default="local",
        help="Model provider mode (default: local deterministic OpenAI/Cortensor-compatible server)",
    )
    demo_parser.add_argument(
        "--deny-dangerous",
        action="store_true",
        help="Simulate operator denying the @tool(dangerous=True) patch tool",
    )
    demo_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full structured JSON execution artifact to stdout",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        args = parser.parse_args(["demo"])

    orchestrator = VeritasOrchestrator(
        provider_mode=args.provider,
        approve_dangerous=not args.deny_dangerous,
    )
    artifact = orchestrator.run_workflow()

    if args.json:
        print(json.dumps(_serialize_artifact(artifact), indent=2))
    else:
        console = Console()
        _render_rich_output(artifact, console)
    return 0


if __name__ == "__main__":
    sys.exit(main())
