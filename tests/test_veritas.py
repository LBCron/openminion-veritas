"""End-to-end and unit test suite for OpenMinion Veritas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openminion_veritas.cli import main as cli_main
from openminion_veritas.engine import (
    VeritasOrchestrator,
    create_sample_buggy_workspace,
)
from openminion_veritas.tools import (
    apply_surgical_patch,
    inspect_source_ast,
    run_pytest_slice,
    scan_security_surface,
)
from repro_issue_703 import verify_issue_703_subagent_decorated_tool


def test_ast_inspection_detects_billing_defects(tmp_path: Path) -> None:
    _, billing_py = create_sample_buggy_workspace(tmp_path)
    report = inspect_source_ast(str(billing_py))
    assert report["syntax_valid"] is True
    assert report["function_count"] == 1
    assert any("broad except Exception" in item for item in report["broad_exception_handlers"])
    assert any("suspicious addition" in item for item in report["potential_defects"])


def test_pytest_slice_runner_and_surgical_patch(tmp_path: Path) -> None:
    target_dir, billing_py = create_sample_buggy_workspace(tmp_path)
    pre = run_pytest_slice(str(target_dir), "test_billing.py")
    assert pre["returncode"] != 0
    assert pre["passed_count"] == 1
    assert pre["failed_count"] == 2

    sec = scan_security_surface(str(billing_py))
    assert sec["clean"] is True

    patch_res = apply_surgical_patch(
        str(billing_py),
        "discounted = subtotal + discount_amount",
        "discounted = max(0.0, subtotal - discount_amount)",
    )
    assert patch_res["patched"] is True

    post = run_pytest_slice(str(target_dir), "test_billing.py")
    assert post["returncode"] == 0
    assert post["passed_count"] == 3
    assert post["failed_count"] == 0


def test_issue_703_subagent_accepts_decorated_tool() -> None:
    outcome = verify_issue_703_subagent_decorated_tool()
    assert outcome["child_tools"] == ["echo_marker"]
    assert outcome["context_tool_allowlist"] == ("echo_marker",)
    assert outcome["inbound_metadata_allowlist"] == "echo_marker"
    assert outcome["reply"] == "verified"


def test_veritas_orchestrator_end_to_end_approved(tmp_path: Path) -> None:
    orchestrator = VeritasOrchestrator(provider_mode="local", approve_dangerous=True)
    artifact = orchestrator.run_workflow(tmp_path)

    assert artifact.pre_patch_pytest["passed_count"] == 1
    assert artifact.pre_patch_pytest["failed_count"] == 2
    assert artifact.post_patch_pytest["passed_count"] == 3
    assert artifact.post_patch_pytest["failed_count"] == 0

    assert artifact.qa_report.verdict == "PASS"
    assert artifact.qa_report.patch_applied is True
    assert artifact.qa_report.tests_passed == 3

    assert artifact.subagent_finding.isolated_context_verified is True
    assert artifact.subagent_finding.risk_level == "LOW"

    assert artifact.handoff_result.reviewer_verdict == "APPROVED"
    assert artifact.handoff_result.regression_passed is True

    patch_approvals = [a for a in artifact.approval_audit if a.tool_name == "apply_surgical_patch"]
    assert len(patch_approvals) == 1
    assert patch_approvals[0].decision == "APPROVED"

    assert artifact.stream_chunk_count > 0
    assert "Veritas Follow-Up Summary" in artifact.streamed_followup_text

    assert artifact.memory_bundle_summary["item_count"] == 3
    assert artifact.memory_bundle_summary["direct_import"]["trust_mode"] == "direct"
    assert artifact.memory_bundle_summary["candidate_import"]["trust_mode"] == "candidate"
    assert artifact.runtime_diagnostics["registered_tool_count"] > 0
    assert artifact.runtime_diagnostics["self_model_present"] is True


def test_veritas_orchestrator_policy_denial(tmp_path: Path) -> None:
    orchestrator = VeritasOrchestrator(provider_mode="local", approve_dangerous=False)
    artifact = orchestrator.run_workflow(tmp_path)

    patch_approvals = [a for a in artifact.approval_audit if a.tool_name == "apply_surgical_patch"]
    assert len(patch_approvals) == 1
    assert patch_approvals[0].decision == "DENIED"

    assert artifact.qa_report.verdict == "NEEDS_REVIEW"
    assert artifact.qa_report.patch_applied is False
    assert artifact.post_patch_pytest["failed_count"] == 2


def test_cli_demo_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(["demo", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["qa_report"]["verdict"] == "PASS"
    assert payload["handoff_result"]["reviewer_verdict"] == "APPROVED"
    assert payload["post_patch_pytest"]["passed_count"] == 3
