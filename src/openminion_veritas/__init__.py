"""OpenMinion Veritas — Autonomous Repository QA, Regression Triage & Patch Verification Agent."""

from openminion_veritas.engine import VeritasExecutionArtifact, VeritasOrchestrator
from openminion_veritas.models import (
    ASTInspectionReport,
    ApprovalAuditRecord,
    PatchVerificationResult,
    QAVerdictReport,
    SecuritySubagentFinding,
)

VeritasRunBundle = VeritasExecutionArtifact

__all__ = [
    "ASTInspectionReport",
    "ApprovalAuditRecord",
    "PatchVerificationResult",
    "QAVerdictReport",
    "SecuritySubagentFinding",
    "VeritasExecutionArtifact",
    "VeritasOrchestrator",
    "VeritasRunBundle",
]
__version__ = "0.1.0"
