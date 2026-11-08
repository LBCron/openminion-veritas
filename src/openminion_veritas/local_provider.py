"""Deterministic OpenAI/Cortensor-compatible local model server for OpenMinion Veritas."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from openminion_veritas.models import (
    PatchVerificationResult,
    QAVerdictReport,
    SecuritySubagentFinding,
)


class VeritasLocalModelServer:
    """In-process OpenAI/Cortensor-compatible `/v1/chat/completions` server.

    Drives the full OpenMinion `APIRuntime` HTTP provider adapter, tool-calling loop,
    `subagent` delegation, `Handoff` routing, `PolicyCtl` operator approval gates,
    SSE streaming (`run_stream`), and Pydantic `output_type` validation without
    requiring external paid API keys.
    """

    def __init__(self, target_file: str, target_dir: str) -> None:
        self.target_file = target_file
        self.target_dir = target_dir
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("VeritasLocalModelServer is not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.endswith("/models"):
                    body = json.dumps(
                        {
                            "object": "list",
                            "data": [{"id": "cortensor-veritas-qa-v1", "object": "model"}],
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw) if raw else {}
                with parent._lock:
                    parent.requests.append(payload)

                if payload.get("stream") is True:
                    stream_text = parent._build_stream_text(payload)
                    chunks = parent._encode_sse_stream(stream_text, payload.get("model", "gpt-4o-mini"))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(chunks)
                    self.wfile.flush()
                    return

                response_obj = parent._route_completion(payload)
                body = json.dumps(response_obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> VeritasLocalModelServer:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _encode_sse_stream(self, text: str, model: str) -> bytes:
        words = text.split(" ")
        lines: list[str] = []
        for idx, word in enumerate(words):
            piece = word if idx == 0 else f" {word}"
            chunk = {
                "id": f"chatcmpl-stream-{idx}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }
                ],
            }
            lines.append(f"data: {json.dumps(chunk)}\n\n")
        final_chunk = {
            "id": "chatcmpl-stream-done",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        lines.append(f"data: {json.dumps(final_chunk)}\n\n")
        lines.append("data: [DONE]\n\n")
        return "".join(lines).encode("utf-8")

    def _build_stream_text(self, payload: dict[str, Any]) -> str:
        messages = payload.get("messages", [])
        user_prompts = [
            str(m.get("content", "")) for m in messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        last_prompt = user_prompts[-1] if user_prompts else ""
        if "executive summary" in last_prompt.lower() or "follow-up" in last_prompt.lower():
            return (
                "Veritas Follow-Up Summary: Discount arithmetic defect in billing.py was "
                "isolated via AST analysis, patched under operator policy approval, and "
                "verified green (3/3 pytest assertions passing)."
            )
        return "Veritas streaming diagnostic channel active and verified."

    def _route_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = payload.get("messages", [])
        tools: list[dict[str, Any]] = payload.get("tools", [])
        tool_names = {
            str(t.get("function", {}).get("name", ""))
            for t in tools
            if isinstance(t, dict) and isinstance(t.get("function"), dict)
        }
        tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        called_tool_names: list[str] = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
                        called_tool_names.append(str(tc["function"].get("name", "")))

        system_text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict) and m.get("role") == "system"
        )
        user_text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )

        # 1. Subagent security scanner flow
        if "security_subagent" in system_text.lower() or (
            "scan_security_surface" in tool_names and "inspect_source_ast" not in tool_names
        ):
            if "scan_security_surface" in tool_names and "scan_security_surface" not in called_tool_names:
                return self._tool_call_response(
                    "call_sec_scan_1",
                    "scan_security_surface",
                    {"file_path": self.target_file},
                )
            finding = SecuritySubagentFinding(
                target_file=self.target_file,
                isolated_context_verified=True,
                risk_level="LOW",
                finding_summary=(
                    "No hardcoded secrets or unsafe eval/exec calls found; "
                    "subagent ran with isolated context and delegated memory policy."
                ),
            )
            return self._text_response(finding.model_dump_json())

        # 2. Handoff Flow (triage coordinator -> patch_reviewer peer agent)
        if "transfer_to_patch_reviewer" in tool_names:
            if "transfer_to_patch_reviewer" not in called_tool_names:
                return self._tool_call_response(
                    "call_handoff_reviewer_1",
                    "transfer_to_patch_reviewer",
                    {"message": f"handoff_verify regression suite in {self.target_dir}"},
                )
            verification = PatchVerificationResult(
                patch_applied=True,
                regression_passed=True,
                reviewer_verdict="APPROVED",
                notes="Peer reviewer confirmed all 3 pytest billing regression tests pass after patch.",
            )
            return self._text_response(verification.model_dump_json())

        if "patch_reviewer" in system_text.lower() or "handoff_verify" in user_text.lower():
            if "run_pytest_slice" in tool_names and "run_pytest_slice" not in called_tool_names:
                return self._tool_call_response(
                    "call_reviewer_pytest_1",
                    "run_pytest_slice",
                    {"target_dir": self.target_dir, "test_file": "test_billing.py"},
                )
            verification = PatchVerificationResult(
                patch_applied=True,
                regression_passed=True,
                reviewer_verdict="APPROVED",
                notes="Peer reviewer confirmed all 3 pytest billing regression tests pass after patch.",
            )
            return self._text_response(verification.model_dump_json())

        if "follow-up executive summary" in user_text.lower():
            return self._text_response(
                "Veritas Follow-Up Summary: Discount arithmetic defect in billing.py was "
                "isolated via AST analysis, patched under operator policy approval, and "
                "verified green (3/3 pytest assertions passing)."
            )

        # 3. Primary QA & Patch Verification Agent multi-step tool flow
        if "inspect_source_ast" in tool_names and "inspect_source_ast" not in called_tool_names:
            return self._tool_call_response(
                "call_ast_1",
                "inspect_source_ast",
                {"file_path": self.target_file},
            )

        if (
            "apply_surgical_patch" in tool_names
            and "apply_surgical_patch" not in called_tool_names
            and "apply_patch" in user_text.lower()
        ):
            return self._tool_call_response(
                "call_patch_1",
                "apply_surgical_patch",
                {
                    "file_path": self.target_file,
                    "old_text": "discounted = subtotal + discount_amount",
                    "new_text": "discounted = max(0.0, subtotal - discount_amount)",
                },
            )

        if (
            "run_pytest_slice" in tool_names
            and "run_pytest_slice" not in called_tool_names
            and ("verify" in user_text.lower() or "apply_patch" in user_text.lower())
        ):
            return self._tool_call_response(
                "call_pytest_1",
                "run_pytest_slice",
                {"target_dir": self.target_dir, "test_file": "test_billing.py"},
            )

        # Check if a tool was denied by operator policy
        denied = any("denied" in str(m.get("content", "")).lower() for m in tool_messages)
        report = QAVerdictReport(
            verdict="NEEDS_REVIEW" if denied else "PASS",
            target_module=self.target_file,
            root_cause=(
                "apply_discount() in billing.py added discount_amount to subtotal "
                "(`subtotal + discount_amount`) instead of subtracting it with a 0.0 floor."
            ),
            defects_found=[
                "line 14: suspicious addition in discount calculation (subtotal + discount_amount)",
                "line 17: broad except Exception handler masking arithmetic errors",
            ],
            patch_applied=not denied,
            tests_passed=1 if denied else 3,
            subagent_summary=(
                "Bounded security subagent verified clean secret/eval surface under isolated context."
            ),
            handoff_verdict="APPROVED" if not denied else "PENDING_APPROVAL",
            recommendations=[
                "Keep @tool(dangerous=True) approval gate enabled for all file-mutating tools.",
                "Replace broad `except Exception` in billing.py with explicit ValueError validation.",
            ],
        )
        return self._text_response(report.model_dump_json())

    @staticmethod
    def _tool_call_response(
        call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{call_id}",
            "object": "chat.completion",
            "created": 1762610000,
            "model": "cortensor-veritas-qa-v1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 64, "completion_tokens": 24, "total_tokens": 88},
        }

    @staticmethod
    def _text_response(content: str) -> dict[str, Any]:
        return {
            "id": "chatcmpl-final",
            "object": "chat.completion",
            "created": 1762610001,
            "model": "cortensor-veritas-qa-v1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                }
            ],
            "usage": {"prompt_tokens": 96, "completion_tokens": 64, "total_tokens": 160},
        }
