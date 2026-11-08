"""Custom OpenMinion @tool definitions for AST static analysis, test execution, and gated patching."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from openminion import tool

from openminion_veritas.models import ASTInspectionReport


@tool(
    description="Inspect a Python source file using the AST to detect defects and exception anti-patterns.",
    min_scope="READ_ONLY",
    idempotent=True,
)
def inspect_source_ast(file_path: str) -> dict[str, object]:
    """Parse a Python source file and return a structured AST inspection summary."""
    target = Path(file_path).resolve()
    if not target.is_file():
        return ASTInspectionReport(
            file_path=str(target),
            syntax_valid=False,
            function_count=0,
            class_count=0,
            potential_defects=[f"File not found: {target}"],
        ).model_dump()

    source = target.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(target))
    except SyntaxError as exc:
        return ASTInspectionReport(
            file_path=str(target),
            syntax_valid=False,
            function_count=0,
            class_count=0,
            potential_defects=[f"SyntaxError at line {exc.lineno}: {exc.msg}"],
        ).model_dump()

    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    broad_handlers: list[str] = []
    defects: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                broad_handlers.append(f"line {node.lineno}: bare except:")
            elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                broad_handlers.append(f"line {node.lineno}: broad except {node.type.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            segment = ast.get_source_segment(source, node) or ""
            if "discount" in segment and "subtotal" in segment:
                defects.append(
                    f"line {node.lineno}: suspicious addition in discount calculation ({segment.strip()})"
                )

    return ASTInspectionReport(
        file_path=str(target),
        syntax_valid=True,
        function_count=len(functions),
        class_count=len(classes),
        broad_exception_handlers=broad_handlers,
        potential_defects=defects,
    ).model_dump()


@tool(
    description="Run a bounded pytest slice inside a target workspace and report pass/fail counts.",
    min_scope="READ_ONLY",
    idempotent=True,
)
def run_pytest_slice(target_dir: str, test_file: str = "test_billing.py") -> dict[str, object]:
    """Execute pytest on a target test file and return structured pass/fail metrics."""
    workdir = Path(target_dir).resolve()
    test_path = workdir / test_file
    if not test_path.is_file():
        return {
            "returncode": 2,
            "passed_count": 0,
            "failed_count": 0,
            "summary": f"Missing test file: {test_path}",
        }

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=short"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    passed_match = re.search(r"(\d+)\s+passed", output)
    failed_match = re.search(r"(\d+)\s+failed", output)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    failed_count = int(failed_match.group(1)) if failed_match else 0

    return {
        "returncode": completed.returncode,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "summary": output.splitlines()[-1] if output.splitlines() else "",
        "output_tail": "\n".join(output.splitlines()[-10:]),
    }


@tool(
    description="Scan a target module for hardcoded credentials, unsafe eval/exec, or patch hygiene risks.",
    min_scope="READ_ONLY",
    idempotent=True,
)
def scan_security_surface(file_path: str) -> dict[str, object]:
    """Perform a static security scan for secrets or unsafe dynamic execution."""
    target = Path(file_path).resolve()
    if not target.is_file():
        return {"file_path": str(target), "clean": False, "findings": ["File not found"]}

    content = target.read_text(encoding="utf-8")
    findings: list[str] = []
    if "eval(" in content or "exec(" in content:
        findings.append("Dynamic code execution (eval/exec) detected")
    if re.search(r"(?:api_key|secret_key|password)\s*=\s*['\"][^'\"]+['\"]", content, re.IGNORECASE):
        findings.append("Potential hardcoded credential detected")

    return {
        "file_path": str(target),
        "clean": len(findings) == 0,
        "findings": findings,
        "line_count": len(content.splitlines()),
    }


@tool(
    description="Apply a surgical text replacement patch to a source file (requires operator approval).",
    dangerous=True,
)
def apply_surgical_patch(file_path: str, old_text: str, new_text: str) -> dict[str, object]:
    """Replace an exact code snippet in a target file. Classified as dangerous=True for policy gating."""
    target = Path(file_path).resolve()
    if not target.is_file():
        return {"patched": False, "error": f"File not found: {target}"}

    content = target.read_text(encoding="utf-8")
    if old_text not in content:
        return {"patched": False, "error": "Target snippet not found in file"}

    updated = content.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    return {
        "patched": True,
        "file_path": str(target),
        "bytes_written": len(updated.encode("utf-8")),
    }
