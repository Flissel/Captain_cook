"""Ende-zu-Ende: Shim-HTTP-Server gegen eine gefaelschte CLI (SHIM_TOOL_CALLS)."""

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fake_claude_cli.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shim = _load("claude_code_openai_shim_e2e", ROOT / "scripts" / "claude_code_openai_shim.py")
tools_mod = _load("claude_code_openai_tools_e2e", ROOT / "scripts" / "claude_code_openai_tools.py")

LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "file-list",
        "description": "List notes in a folder",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
VALID_BLOCK = '<tool_calls>\n[{"name": "file-list", "arguments": {"path": "knowledge/People"}}]\n</tool_calls>'
INVALID_BLOCK = '<tool_calls>\n[{"name": "file-list", "arguments": {}}]\n</tool_calls>'
USER = {"role": "user", "content": "Welche Notizen liegen unter knowledge/People?"}
SYSTEM = {"role": "system", "content": "Du bist der Copilot."}


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.log = tmp_path / "calls.jsonl"
        self.replies = tmp_path / "replies.json"
        wrapper = tmp_path / "fake-claude.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(wrapper))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CLAUDE_REPLIES", str(self.replies))
        for name in (
            "SHIM_TOOL_CALLS",
            "CAPTAIN_ARTIFACT_ROOT",
            "SHIM_EXTRA_MCP_CONFIG",
            "SHIM_DUMP_DIR",
            "SHIM_NEUTRALIZE_DOUBLE_BRACKETS",
            "FAKE_CLAUDE_DELAY",
        ):
            monkeypatch.delenv(name, raising=False)
        self.monkeypatch = monkeypatch
        self.server = None
        self.timeout = 30.0

    def start(self, replies: list, *, tool_calls: bool, timeout: float = 30.0) -> None:
        self.replies.write_text(json.dumps(replies), encoding="utf-8")
        if self.log.exists():
            self.log.unlink()
        if tool_calls:
            self.monkeypatch.setenv("SHIM_TOOL_CALLS", "1")
        else:
            self.monkeypatch.delenv("SHIM_TOOL_CALLS", raising=False)
        self.stop()
        handler = type("TestHandler", (shim.Handler,), {"timeout_seconds": timeout})
        self.server = shim.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.server_address[1]

    def post(self, body: dict) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.stop()


def _argv_without_tempfile(argv: list[str]) -> list[str]:
    """Der Name der System-Prompt-Datei ist pro Aufruf zufaellig."""
    out = list(argv)
    if "--system-prompt-file" in out:
        out[out.index("--system-prompt-file") + 1] = "<tmp>"
    return out


def _strip_volatile(raw: bytes) -> dict:
    data = json.loads(raw)
    data.pop("id", None)
    data.pop("created", None)
    return data


def _sse(raw: bytes) -> list:
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        if block.startswith("data: "):
            payload = block[len("data: "):]
            events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _request(*, tools: bool = True, stream: bool = False, tool_choice=None, messages=None) -> dict:
    body: dict = {"model": "claude-code", "messages": messages or [SYSTEM, USER]}
    if tools:
        body["tools"] = [LIST_TOOL]
    if stream:
        body["stream"] = True
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


TOOL_HISTORY = [
    SYSTEM,
    USER,
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_abc", "type": "function", "function": {"name": "file-list", "arguments": '{"path": "knowledge/People"}'}}
        ],
    },
    {"role": "tool", "tool_call_id": "call_abc", "content": '["Anna.md", "Ben.md"]'},
]


# ---------------------------------------------------------------- Schalter aus
def test_switch_off_byte_identical(rig):
    rig.start(["Hallo"], tool_calls=False)
    status_a, raw_a = rig.post(_request(tools=False, messages=TOOL_HISTORY))
    status_b, raw_b = rig.post(_request(tools=True, messages=TOOL_HISTORY, tool_choice="required"))
    assert status_a == status_b == 200
    assert _strip_volatile(raw_a) == _strip_volatile(raw_b)
    # Key-Reihenfolge und Serialisierung identisch, nicht nur der Inhalt.
    a, b = json.loads(raw_a), json.loads(raw_b)
    b["id"], b["created"] = a["id"], a["created"]
    assert json.dumps(b).encode("utf-8") == raw_a

    calls = rig.calls()
    assert len(calls) == 2
    assert _argv_without_tempfile(calls[0]["argv"]) == _argv_without_tempfile(calls[1]["argv"])
    assert calls[0]["stdin"] == calls[1]["stdin"]
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"] == "Du bist der Copilot."
    assert "--tools" not in calls[1]["argv"]
    # Verlauf exakt wie das alte render_messages.
    assert calls[1]["stdin"] == shim.render_messages(TOOL_HISTORY)[1]

    # Streaming ohne Schalter: alte Einzel-Chunk-Form.
    status, raw = rig.post(_request(tools=True, stream=True))
    events = _sse(raw)
    assert events[0]["choices"][0]["delta"] == {"role": "assistant", "content": "Hallo"}
    assert events[1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1] == "[DONE]"


# ----------------------------------------------------------------- Schalter an
def test_tool_call_nonstream(rig):
    rig.start([VALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request())
    assert status == 200
    data = json.loads(raw)
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "file-list"
    assert json.loads(call["function"]["arguments"]) == {"path": "knowledge/People"}

    calls = rig.calls()
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert calls[0]["system_prompt"].startswith("Du bist der Copilot.\n\n# Verfuegbare Werkzeuge")
    assert "## file-list" in calls[0]["system_prompt"]


def test_tool_call_stream(rig):
    rig.start([VALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request(stream=True))
    assert status == 200
    events = _sse(raw)
    assert len(events) == 3
    delta = events[0]["choices"][0]["delta"]
    assert delta["tool_calls"][0]["index"] == 0
    assert delta["tool_calls"][0]["function"]["name"] == "file-list"
    assert events[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[2] == "[DONE]"


def test_tool_result_round(rig):
    rig.start(["Unter knowledge/People liegen Anna.md und Ben.md."], tool_calls=True)
    status, raw = rig.post(_request(messages=TOOL_HISTORY))
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["content"] == "Unter knowledge/People liegen Anna.md und Ben.md."
    stdin = rig.calls()[0]["stdin"]
    assert 'Assistant (Werkzeugaufruf): file-list({"path": "knowledge/People"}) [call_abc]' in stdin
    assert 'Werkzeugergebnis [call_abc, file-list]: ["Anna.md", "Ben.md"]' in stdin


def test_invalid_then_corrected(rig):
    rig.start([INVALID_BLOCK, VALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request())
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    calls = rig.calls()
    assert len(calls) == 2
    assert "Dein letzter Werkzeugaufruf war ungueltig" in calls[1]["stdin"]
    assert 'Pflichtfeld "path" fehlt' in calls[1]["stdin"]
    assert calls[1]["stdin"].startswith(calls[0]["stdin"])
    assert "Dein letzter Werkzeugaufruf" not in calls[0]["stdin"]


def test_invalid_twice_falls_back(rig):
    rig.start(["Ich schaue nach.\n" + INVALID_BLOCK, INVALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request())
    data = json.loads(raw)
    content = data["choices"][0]["message"]["content"]
    assert data["choices"][0]["finish_reason"] == "stop"
    assert content == tools_mod.TOOL_FAIL_TEXT
    assert "<tool_calls>" not in raw.decode("utf-8")
    assert len(rig.calls()) == 2


def test_invalid_twice_keeps_outside_text(rig):
    rig.start([INVALID_BLOCK, "Ich schaue nach.\n" + INVALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request(stream=True))
    events = _sse(raw)
    assert events[0]["choices"][0]["delta"]["content"] == "Ich schaue nach."
    assert "<tool_calls>" not in raw.decode("utf-8")


def test_no_retry_when_time_short(rig, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "1.5")
    rig.start([INVALID_BLOCK, VALID_BLOCK], tool_calls=True, timeout=2.5)
    status, raw = rig.post(_request())
    data = json.loads(raw)
    assert data["choices"][0]["message"]["content"] == tools_mod.TOOL_FAIL_TEXT
    assert len(rig.calls()) == 1


def test_tool_choice_none_strips_block(rig):
    rig.start(["Vorab.\n" + VALID_BLOCK], tool_calls=True)
    status, raw = rig.post(_request(tool_choice="none"))
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["content"] == "Vorab."
    calls = rig.calls()
    assert len(calls) == 1
    assert "Verfuegbare Werkzeuge" not in calls[0]["system_prompt"]


def test_required_but_text_returns_text(rig, capsys):
    rig.start(["Nur Text."], tool_calls=True)
    status, raw = rig.post(_request(tool_choice="required"))
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["content"] == "Nur Text."
    assert "Pflicht: Antworte jetzt mit mindestens einem Werkzeugaufruf" in rig.calls()[0]["system_prompt"]
    assert "tool_choice=required" in capsys.readouterr().err


def test_plain_text_is_passed_through_unchanged(rig):
    rig.start(["  Antwort mit Rand.\n"], tool_calls=True)
    status, raw = rig.post(_request())
    assert json.loads(raw)["choices"][0]["message"]["content"] == "  Antwort mit Rand.\n"


def _raw_post(port: int, body: dict) -> socket.socket:
    data = json.dumps(body).encode("utf-8")
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.sendall(
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        + data
    )
    return sock


def _reset(sock: socket.socket) -> None:
    # SO_LINGER 0: close() schickt RST, wie ein abgebrochener Client.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _wait_for_handler(rig, capsys, needle: str, seconds: float = 15.0) -> str:
    err = ""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        err += capsys.readouterr().err
        if needle in err:
            break
        time.sleep(0.05)
    time.sleep(0.3)  # der Handler darf nach der Zeile nichts mehr loggen
    return err + capsys.readouterr().err


def test_stream_client_disconnect_is_quiet(rig, capsys):
    rig.start([VALID_BLOCK], tool_calls=True)
    sock = _raw_post(rig.port, _request(stream=True))
    first = sock.recv(64)  # erster Teil der Antwort ist da
    assert first.startswith(b"HTTP/1.")
    _reset(sock)
    err = _wait_for_handler(rig, capsys, '"POST /v1/chat/completions HTTP/1.1" 200')
    assert "Traceback" not in err
    assert err.count("client disconnected") <= 1


def test_client_gone_before_answer_logs_one_line(rig, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "1.0")
    rig.start([VALID_BLOCK], tool_calls=True)
    sock = _raw_post(rig.port, _request(stream=True))
    time.sleep(0.3)  # Anfrage ist angekommen, die CLI laeuft noch
    _reset(sock)
    err = _wait_for_handler(rig, capsys, "client disconnected")
    assert "Traceback" not in err
    assert err.count("client disconnected") == 1


def test_round_log_line(rig, capsys):
    rig.start([INVALID_BLOCK, VALID_BLOCK], tool_calls=True)
    rig.post(_request())
    err = capsys.readouterr().err
    assert "round: n=1 wall_ms=" in err
    assert "api_ms=1234 cache_read=77 outcome=retry" in err
    assert "round: n=2 " in err and "outcome=tool_calls" in err


def test_run_claude_flag_only_when_requested(rig):
    rig.start(["x", "x"], tool_calls=False)
    shim.run_claude(system_prompt="s", transcript="t", model=None, timeout=30)
    shim.run_claude(system_prompt="s", transcript="t", model=None, timeout=30, disable_builtin_tools=True)
    plain, disabled = (_argv_without_tempfile(c["argv"]) for c in rig.calls())
    assert "--tools" not in plain
    assert disabled[: len(plain) - 2] == plain[:-2]
    assert disabled[disabled.index("--tools") + 1] == ""
