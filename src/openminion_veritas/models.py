"""Typed Pydantic schemas for OpenMinion Veritas structured outputs and audit records."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ASTInspectionReport(BaseModel):
    """Structured result from static AST inspection of a target Python module."""

    file_path: str
    syntax_valid: bool
    function_count: int
    class_count: int
    broad_exception_handlers: list[str] = Field(default_factory=list)
    potential_defects: list[str] = Field(default_factory=list)


class SecuritySubagentFinding(BaseModel):
    """Structured output returned by the bounded security/isolation subagent."""

    target_file: str
    isolated_context_verified: bool
    risk_level: str
    finding_summary: str


class PatchVerificationResult(BaseModel):
    """Structured output returned by the peer patch-reviewer handoff agent."""

    patch_applied: bool
    regression_passed: bool
    reviewer_verdict: str
    notes: str


class ApprovalAuditRecord(BaseModel):
    """Operator policy confirmation event captured during an Agent turn."""

    approval_id: str
    tool_name: str
    arguments: dict[str, object]
    decision: str


class QAVerdictReport(BaseModel):
    """Final typed QA & Patch Verification report validated by Agent(output_type=QAVerdictReport)."""

    verdict: str = Field(description="PASS, FAIL, or NEEDS_REVIEW")
    target_module: str
    root_cause: str
    defects_found: list[str]
    patch_applied: bool
    tests_passed: int
    subagent_summary: str
    handoff_verdict: str
    recommendations: list[str]
