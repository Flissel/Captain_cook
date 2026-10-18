"""Werkzeugaufrufe fuer den Claude-Code-OpenAI-Shim (rein, ohne HTTP und CLI).

Die CLI kennt keinen Werkzeugmodus fuer fremde Werkzeuge. Der Vertrag laeuft
deshalb ueber Text: Der Prompt nennt die Werkzeuge, Claude antwortet entweder
normal oder ausschliesslich mit einem Block

    <tool_calls>
    [{"name": "<werkzeug>", "arguments": {...}}]
    </tool_calls>

Dieses Modul erzeugt den Prompt-Abschnitt, parst und prueft die Antwort,
rendert den Verlauf (inkl. Werkzeugnachrichten) und baut die OpenAI-Formen.
Nur Standardbibliothek.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

TOOL_FAIL_TEXT = "Ich konnte das benoetigte Werkzeug nicht korrekt aufrufen."
RESULT_LIMIT = 20000

_OPEN_TAG = "<tool_calls>"
_CLOSE_TAG = "</tool_calls>"
_BLOCK_RE = re.compile(r"<tool_calls>(.*?)</tool_calls>", re.DOTALL)
# Code-Zaun direkt um den ganzen Block (```json ... ```).
_FENCED_BLOCK_RE = re.compile(
    r"```[A-Za-z0-9_-]*[ \t]*\r?\n?\s*(<tool_calls>.*?</tool_calls>)\s*```", re.DOTALL
)
_LEADING_FENCE_RE = re.compile(r"\s*```[A-Za-z0-9_-]*[ \t]*\r?\n?")
_LINE_START_OPEN_RE = re.compile(r"(?m)^[ \t]*<tool_calls>")
_INNER_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\s*```$", re.DOTALL)
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


# ---------------------------------------------------------------- Werkzeuge
def normalize_tools(tools: list | None) -> list[dict]:
    """OpenAI-Form (``{type: function, function: {...}}``) und flache Form -> einheitlich."""

    out: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        spec = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}}
        description = spec.get("description")
        out.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": parameters,
            }
        )
    return out


def tool_choice_mode(tool_choice: Any) -> tuple[str, str | None]:
    """-> (``none`` | ``auto`` | ``required``, erzwungener Name oder None)."""

    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return "none", None
        if tool_choice == "required":
            return "required", None
        return "auto", None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "none":
            return "none", None
        spec = tool_choice.get("function")
        name = spec.get("name") if isinstance(spec, dict) else tool_choice.get("name")
        if isinstance(name, str) and name:
            return "required", name
        if tool_choice.get("type") == "required":
            return "required", None
    return "auto", None


_CONTRACT = (
    "Antwortvertrag fuer Werkzeuge. Du fuehrst Werkzeuge NICHT selbst aus und "
    "erfindest nie ein Werkzeugergebnis. Ergebnisse stehen nur im Verlauf als "
    "\"Werkzeugergebnis [...]\". Brauchst du ein Werkzeug, antworte AUSSCHLIESSLICH "
    "mit genau einem Block und ohne Text davor oder danach:\n"
    "<tool_calls>\n"
    "[{\"name\": \"<werkzeug>\", \"arguments\": {...}}]\n"
    "</tool_calls>\n"
    "Der Block enthaelt ein JSON-Array; jedes Element hat \"name\" (ein Name aus der "
    "Liste unten) und \"arguments\" (ein JSON-Objekt, das zum Schema passt, mit allen "
    "Pflichtfeldern). Mehrere unabhaengige Aufrufe gehoeren in dasselbe Array. "
    "Verwende keinen Code-Zaun und nur einen einzigen Block. "
    "Schreibe das Tag <tool_calls> niemals in Erklaerungen oder Beispielen. "
    "Kopiere nie die Verlaufszeilen 'Assistant (Werkzeugaufruf): ...' - rufe "
    "Werkzeuge nur ueber den Block auf."
)


def tools_prompt_section(tools: list[dict], mode: str, forced: str | None) -> str:
    if mode == "none" or not tools:
        return ""
    lines = ["# Verfuegbare Werkzeuge", "", _CONTRACT, ""]
    if forced:
        lines.append(
            f"Pflicht: Antworte jetzt mit einem Werkzeugaufruf an \"{forced}\", nicht mit Text."
        )
    elif mode == "required":
        lines.append("Pflicht: Antworte jetzt mit mindestens einem Werkzeugaufruf, nicht mit Text.")
    else:
        lines.append(
            "Brauchst du kein Werkzeug, antworte ganz normal als Text, ohne den Block."
        )
    lines.append("")
    for tool in tools:
        lines.append(f"## {tool['name']}")
        if tool.get("description"):
            lines.append(tool["description"])
        lines.append("Parameter (JSON-Schema): " + json.dumps(tool["parameters"], ensure_ascii=False))
        lines.append("")
    return "\n".join(lines).rstrip()


def correction_prompt(errors: list[str]) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        "Dein letzter Werkzeugaufruf war ungueltig und wurde nicht ausgefuehrt:\n"
        f"{listed}\n\n"
        "Antworte jetzt erneut. Entweder gibst du einen korrekten Block "
        "<tool_calls>[{\"name\": \"...\", \"arguments\": {...}}]</tool_calls> aus "
        "(ausschliesslich der Block, nur Namen aus der Werkzeugliste, alle "
        "Pflichtfelder, passende Typen), oder du antwortest als normaler Text."
    )


def required_correction_prompt(forced: str | None) -> str:
    """Korrektursatz, wenn ``tool_choice`` einen Aufruf verlangt, aber Text kam."""

    target = f"Ein Werkzeugaufruf an \"{forced}\"" if forced else "Ein Werkzeugaufruf"
    return (
        f"{target} ist hier Pflicht: antworte ausschliesslich mit dem <tool_calls>-Block, "
        "ohne Text davor oder danach."
    )


# ------------------------------------------------------------------ Verlauf
def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _args_text(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(arguments)


def _limit(text: str) -> str:
    if len(text) <= RESULT_LIMIT:
        return text
    return f"{text[:RESULT_LIMIT]} (gekuerzt, {len(text)} Zeichen)"


def render_messages_with_tools(messages: list[dict]) -> tuple[str, str]:
    """Wie ``render_messages`` des Shims, plus ``assistant.tool_calls`` und ``role: tool``."""

    system_parts: list[str] = []
    turns: list[str] = []
    names_by_id: dict[str, str] = {}
    for message in messages or []:
        role = str(message.get("role", "user"))
        content = _content_text(message.get("content", ""))
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            tool_calls = message.get("tool_calls")
            valid = [c for c in tool_calls if isinstance(c, dict)] if isinstance(tool_calls, list) else []
            if not valid:
                turns.append(f"Assistant: {content}")
                continue
            if content.strip():
                turns.append(f"Assistant: {content}")
            for call in valid:
                spec = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(spec.get("name") or "unbekannt")
                call_id = str(call.get("id") or "")
                if call_id:
                    names_by_id[call_id] = name
                turns.append(
                    f"Assistant (Werkzeugaufruf): {name}({_args_text(spec.get('arguments'))}) [{call_id}]"
                )
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = names_by_id.get(call_id) or str(message.get("name") or "") or "unbekannt"
            turns.append(f"Werkzeugergebnis [{call_id}, {name}]: {_limit(content)}")
        else:
            turns.append(f"{role.capitalize()}: {content}")

    transcript = "\n\n".join(turns).strip()
    if not transcript:
        transcript = "(no user message)"
    return "\n\n".join(system_parts).strip(), transcript


# ------------------------------------------------------------------- Parser
@dataclass
class ParsedReply:
    calls: list[dict] = field(default_factory=list)
    text: str = ""
    errors: list[str] = field(default_factory=list)
    had_block: bool = False


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _validate_call(index: int, item: Any, by_name: dict[str, dict]) -> tuple[dict | None, list[str]]:
    label = f"Aufruf {index + 1}"
    if not isinstance(item, dict):
        return None, [f"{label}: kein JSON-Objekt"]
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None, [f"{label}: \"name\" fehlt oder ist kein String"]
    tool = by_name.get(name)
    if tool is None:
        return None, [f"{label}: Unbekanntes Werkzeug \"{name}\" (nicht angeboten)"]
    arguments = item.get("arguments", {}) if "arguments" not in item else item["arguments"]
    if not isinstance(arguments, dict):
        return None, [f"{label} ({name}): \"arguments\" fehlt oder ist kein JSON-Objekt"]

    errors: list[str] = []
    schema = tool.get("parameters") or _EMPTY_SCHEMA
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for key in required:
        if isinstance(key, str) and key not in arguments:
            errors.append(f"{label} ({name}): Pflichtfeld \"{key}\" fehlt")
    for key, value in arguments.items():
        spec = properties.get(key)
        expected = spec.get("type") if isinstance(spec, dict) else None
        types = [expected] if isinstance(expected, str) else expected if isinstance(expected, list) else []
        checks = [_TYPE_CHECKS[t] for t in types if t in _TYPE_CHECKS]
        if checks and not any(check(value) for check in checks):
            errors.append(
                f"{label} ({name}): Feld \"{key}\" hat den falschen Typ (erwartet {'/'.join(map(str, types))})"
            )
    if errors:
        return None, errors
    return {"id": f"call_{uuid.uuid4().hex[:24]}", "name": name, "arguments": arguments}, []


def _cut_at_tag(text: str) -> str:
    """Text vor dem ersten Tag; ein dabei offen gebliebener Code-Zaun faellt weg."""

    positions = [i for i in (text.find(_OPEN_TAG), text.find(_CLOSE_TAG)) if i >= 0]
    head = text[: min(positions)] if positions else text
    if head.count("```") % 2 == 1:
        head = head[: head.rfind("```")]
    return head.strip()


def parse_reply(text: str, tools: list[dict]) -> ParsedReply:
    """Wie ``_parse_reply``, plus Endwache: kein Tag erreicht je den Text."""

    parsed = _parse_reply(text, tools)
    if _OPEN_TAG in parsed.text or _CLOSE_TAG in parsed.text:
        errors = parsed.errors or ["Ein <tool_calls>-Tag stand ausserhalb eines gueltigen Blocks"]
        return ParsedReply(calls=[], text=_cut_at_tag(parsed.text), errors=errors, had_block=True)
    return parsed


def _parse_reply(text: str, tools: list[dict]) -> ParsedReply:
    """Sucht genau einen ``<tool_calls>``-Block, prueft ihn und trennt den Begleittext ab.

    Bei jedem Fehler bleibt ``calls`` leer: ein Teil-Plan wird nie weitergegeben.
    """

    source = text if isinstance(text, str) else ""
    # Ein Code-Zaun zaehlt nur, wenn er die Antwort eroeffnet (kein Text davor).
    fenced = _FENCED_BLOCK_RE.search(source)
    if fenced and not source[: fenced.start()].strip():
        source = fenced.group(1) + source[fenced.end():]
    else:
        # Ein Zaun, der die Antwort eroeffnet, ist deren Huelle - auch ungeschlossen.
        opener = _LEADING_FENCE_RE.match(source)
        if opener and _OPEN_TAG in source[opener.end():]:
            source = source[opener.end():]
            if source.rstrip().endswith("```"):
                source = source.rstrip()[:-3]

    first = -1
    for m in _LINE_START_OPEN_RE.finditer(source):
        start = m.end() - len(_OPEN_TAG)
        if source[:start].count("```") % 2 == 0:  # nicht in einem Zaun mitten im Text
            first = start
            break
    if first < 0:
        return ParsedReply(text=source.strip())  # Endwache in parse_reply prueft Tags

    head, rest = source[:first], source[first:]
    matches = list(_BLOCK_RE.finditer(rest))
    errors: list[str] = []
    if len(matches) > 1 or rest.count(_OPEN_TAG) > 1:
        errors.append("Mehr als ein <tool_calls>-Block; erlaubt ist genau einer")
    elif not matches:
        errors.append("Der <tool_calls>-Block ist nicht geschlossen oder unvollstaendig")
    else:
        body = matches[0].group(1).strip()
        fence = _INNER_FENCE_RE.match(body)
        if fence:
            body = fence.group(1).strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            errors.append(f"Kein gueltiges JSON im Block: {exc.msg} (Zeile {exc.lineno}, Spalte {exc.colno})")
            payload = None
        if payload is not None:
            if not isinstance(payload, list) or not payload:
                errors.append("Der Block muss ein nicht leeres JSON-Array von Aufrufen enthalten")
            else:
                by_name = {t["name"]: t for t in tools}
                calls: list[dict] = []
                for index, item in enumerate(payload):
                    call, call_errors = _validate_call(index, item, by_name)
                    errors.extend(call_errors)
                    if call is not None:
                        calls.append(call)
                if not errors:
                    outside = head + _BLOCK_RE.sub("", rest)
                    return ParsedReply(calls=calls, text=outside.strip(), errors=[], had_block=True)
    # Fehlerpfad: nur der Text VOR dem ersten Tag, nie Nutzlast oder Tags.
    return ParsedReply(
        calls=[], text=head.replace(_CLOSE_TAG, "").strip(), errors=errors, had_block=True
    )


# -------------------------------------------------------------- OpenAI-Form
def completion_with_tool_calls(calls: list[dict], model: str, usage: dict) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


def stream_chunks_for_tool_calls(completion: dict) -> list[dict]:
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    message = completion["choices"][0]["message"]
    tool_calls = [
        {
            "index": index,
            "id": call["id"],
            "type": "function",
            "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
        }
        for index, call in enumerate(message.get("tool_calls") or [])
    ]
    first = dict(base)
    first["choices"] = [
        {"index": 0, "delta": {"role": "assistant", "tool_calls": tool_calls}, "finish_reason": None}
    ]
    last = dict(base)
    last["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
    return [first, last]
