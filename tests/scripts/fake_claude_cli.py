"""Gefaelschte Claude-Code-CLI fuer die Shim-Tests (nur Standardbibliothek).

- ``FAKE_CLAUDE_REPLIES``: Pfad zu einem JSON-Array. Jedes Element ist der
  ``result``-Text eines Aufrufs (String) oder ein Objekt, das ueber die
  Standardausgabe gelegt wird. Aufruf n bekommt Element n (das letzte
  wiederholt sich).
- ``FAKE_CLAUDE_LOG``: Pfad zu einer JSONL-Datei; je Aufruf eine Zeile mit
  ``argv``, ``stdin`` und dem Inhalt der ``--system-prompt-file``.
- ``FAKE_CLAUDE_DELAY``: optionale Verzoegerung in Sekunden vor der Antwort.
"""

import json
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    system_prompt = None
    if "--system-prompt-file" in argv:
        path = argv[argv.index("--system-prompt-file") + 1]
        with open(path, encoding="utf-8") as handle:
            system_prompt = handle.read()

    log_path = os.environ["FAKE_CLAUDE_LOG"]
    calls = 0
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as handle:
            calls = sum(1 for line in handle if line.strip())
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "stdin": stdin, "system_prompt": system_prompt}) + "\n")

    with open(os.environ["FAKE_CLAUDE_REPLIES"], encoding="utf-8") as handle:
        replies = json.load(handle)
    reply = replies[min(calls, len(replies) - 1)]

    delay = float(os.environ.get("FAKE_CLAUDE_DELAY") or 0)
    if delay:
        time.sleep(delay)

    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "stop_reason": "end_turn",
        "duration_api_ms": 1234,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 77},
    }
    if isinstance(reply, dict):
        payload.update(reply)
    else:
        payload["result"] = reply
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
