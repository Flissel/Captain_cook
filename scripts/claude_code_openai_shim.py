"""OpenAI-compatible HTTP shim backed by the Claude Code CLI subscription.

Hermes' ``custom`` provider speaks OpenAI's wire format and resolves its
endpoint from ``OPENAI_BASE_URL`` (see ``_resolve_custom_runtime`` in the
Hermes agent).  This server implements just enough of that surface to let
Captain's Hermes planner run on a Claude Code subscription instead of a
metered API key:

    GET  /v1/models             -> advertises the shim's model ids
    POST /v1/chat/completions   -> runs ``claude -p --output-format json``

Every request runs the CLI in an empty working directory with project and
user settings switched off, so a planning call does not drag the ~44k token
repository context into the subscription on every turn.

Standard library only, so it runs anywhere the CLI does.

Usage:
    python scripts/claude_code_openai_shim.py [--host 127.0.0.1] [--port 8114]

Then point Hermes at it:
    OPENAI_BASE_URL=http://127.0.0.1:8114/v1
    OPENAI_API_KEY=<any non-empty placeholder>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODEL_ID = "claude-code"
# Model ids we advertise. The bare id lets the CLI pick the account default;
# the explicit aliases let a caller pin a tier.
ADVERTISED_MODELS = (DEFAULT_MODEL_ID, "claude-code-opus", "claude-code-sonnet")
_MODEL_ALIASES = {
    "claude-code-opus": "opus",
    "claude-code-sonnet": "sonnet",
    "claude-code-haiku": "haiku",
}


class ShimError(RuntimeError):
    """Raised when the CLI could not produce a usable answer."""


def resolve_cli() -> str:
    explicit = os.environ.get("CLAUDE_CODE_CLI", "").strip()
    if explicit:
        return explicit
    # Prefer the native install over PATH: a stale npm launcher can sit earlier
    # on PATH and fail with "claude.cmd not found" while this one works.
    for candidate in (
        os.path.expanduser(r"~\.local\bin\claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("claude")
    if found:
        return found
    raise ShimError("no Claude Code CLI found (set CLAUDE_CODE_CLI)")


def render_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Split OpenAI messages into (system prompt, conversation transcript)."""

    system_parts: list[str] = []
    turns: list[str] = []
    for message in messages or []:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            # OpenAI content parts: keep the text ones, in order.
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        content = str(content)
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            turns.append(f"Assistant: {content}")
        else:
            turns.append(f"{role.capitalize()}: {content}")

    transcript = "\n\n".join(turns).strip()
    if not transcript:
        transcript = "(no user message)"
    return "\n\n".join(system_parts).strip(), transcript


def run_claude(
    *,
    system_prompt: str,
    transcript: str,
    model: str | None,
    timeout: float,
) -> dict[str, Any]:
    cli = resolve_cli()
    argv: list[str] = [cli, "-p", "--output-format", "json"]

    # The system prompt goes through a file, never the command line: a real
    # agent system prompt runs to tens of thousands of characters and Windows
    # caps a whole command line at 32767, where the CLI aborts before it reads
    # any input (observed: is_error with 0 input tokens after ~800ms).
    system_prompt_file: str | None = None
    if system_prompt:
        handle, system_prompt_file = tempfile.mkstemp(
            prefix="claude-shim-system-", suffix=".txt"
        )
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(system_prompt)
        argv += ["--system-prompt-file", system_prompt_file]
    resolved_model = _MODEL_ALIASES.get((model or "").strip())
    if resolved_model:
        argv += ["--model", resolved_model]

    # Keep the call cheap and deterministic: no MCP servers, no inherited
    # project/user settings, no tools. A planner turn only needs text back.
    argv += ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}})]
    argv += ["--setting-sources", ""]

    environment = os.environ.copy()
    # Never let an ambient key silently redirect the CLI itself.
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("ANTHROPIC_AUTH_TOKEN", None)

    try:
        with tempfile.TemporaryDirectory(prefix="claude-shim-") as workdir:
            try:
                completed = subprocess.run(
                    argv,
                    input=transcript,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=workdir,
                    env=environment,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ShimError(f"Claude Code CLI timed out after {timeout}s") from exc
    finally:
        if system_prompt_file:
            try:
                os.unlink(system_prompt_file)
            except OSError:
                pass

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        sys.stderr.write(f"cli rc={completed.returncode} output={detail[:4000]}\n")
        sys.stderr.flush()
        raise ShimError(f"Claude Code CLI exited {completed.returncode}: {detail[:500]}")

    raw = (completed.stdout or "").strip()
    if not raw:
        # The CLI reports some failures on stdout with a zero exit status, so an
        # empty stdout is its own distinct fault worth naming.
        raise ShimError("Claude Code CLI produced no output")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ShimError(f"Claude Code CLI returned non-JSON output: {raw[:500]}") from exc

    if payload.get("is_error"):
        raise ShimError(f"Claude Code reported an error: {str(payload.get('result'))[:500]}")
    return payload


def to_completion(payload: dict[str, Any], model: str) -> dict[str, Any]:
    text = payload.get("result")
    if not isinstance(text, str):
        raise ShimError("Claude Code result did not contain text")

    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0) + int(
        usage.get("cache_read_input_tokens") or 0
    )
    completion_tokens = int(usage.get("output_tokens") or 0)

    stop_reason = str(payload.get("stop_reason") or "end_turn")
    finish_reason = "length" if stop_reason == "max_tokens" else "stop"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeCodeOpenAIShim/1.0"
    timeout_seconds = 600.0

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -------------------------------------------------------
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": {"message": message, "type": "shim_error"}})

    def _send_stream(self, completion: dict[str, Any]) -> None:
        """Emit the answer as a single SSE chunk followed by [DONE]."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        content = completion["choices"][0]["message"]["content"]
        chunk = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        }
        final = dict(chunk)
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        for item in (chunk, final):
            self.wfile.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # -- routes --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model_id,
                            "object": "model",
                            "created": 0,
                            "owned_by": "anthropic",
                        }
                        for model_id in ADVERTISED_MODELS
                    ],
                },
            )
            return
        if path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok"})
            return
        self._send_error(404, f"unknown path {path}")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error(404, f"unknown path {path}")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_error(400, f"invalid request body: {exc}")
            return

        model = str(body.get("model") or DEFAULT_MODEL_ID)
        system_prompt, transcript = render_messages(body.get("messages") or [])

        roles = [str(m.get("role", "?")) for m in (body.get("messages") or [])]
        sys.stderr.write(
            "request: model=%s roles=%s system=%dch transcript=%dch stream=%s tools=%d\n"
            % (
                model,
                ",".join(roles),
                len(system_prompt),
                len(transcript),
                bool(body.get("stream")),
                len(body.get("tools") or []),
            )
        )
        sys.stderr.flush()

        try:
            payload = run_claude(
                system_prompt=system_prompt,
                transcript=transcript,
                model=model,
                timeout=self.timeout_seconds,
            )
            completion = to_completion(payload, model)
        except ShimError as exc:
            self._send_error(502, str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._send_error(500, f"unexpected shim failure: {exc}")
            return

        if body.get("stream"):
            self._send_stream(completion)
            return
        self._send_json(200, completion)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8114)
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds to wait for one CLI turn (default: 600)",
    )
    args = parser.parse_args()

    Handler.timeout_seconds = args.timeout
    cli = resolve_cli()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        f"claude-code-openai-shim listening on http://{args.host}:{args.port}/v1 "
        f"(cli: {cli})\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
