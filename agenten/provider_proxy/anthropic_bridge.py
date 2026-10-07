"""Pure translation between the Anthropic Messages API and OpenAI-style chat completions.

Claude Code speaks the Messages API; Nebius Token Factory serves chat
completions. Nothing here performs I/O. Unsupported Anthropic-only fields
(thinking, output_config, metadata, cache_control, betas) are dropped.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_TOOL_NAME = 64


class BridgeError(ValueError):
    """The client request cannot be translated (maps to invalid_request_error)."""


class UpstreamFormatError(RuntimeError):
    """The upstream chat response is malformed (maps to api_error)."""


def short_tool_name(name: str) -> str:
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME - 9]}_{digest}"


@dataclass(frozen=True)
class ChatTranslation:
    payload: dict[str, object]
    tool_names: Mapping[str, str]


def _require_str(block: Mapping[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str):
        raise BridgeError(f"block field {key!r} must be a string")
    return value


def _system_text(system: object) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        raise BridgeError("system must be a string or a list of text blocks")
    parts = []
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise BridgeError("system blocks must be text")
        parts.append(_require_str(block, "text"))
    return "\n\n".join(parts)


def _tool_result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise BridgeError("tool_result content must be text (image blocks are not supported)")
            parts.append(_require_str(item, "text"))
        text = "\n".join(parts)
    else:
        raise BridgeError("tool_result content must be a string or a list")
    return f"Error: {text}" if block.get("is_error") else text


def _translate_message(message: object, chat_name: Callable[[str], str]) -> tuple[list[dict[str, object]], set[str]]:
    """Translate a message and return (messages, tool_call_ids_produced)."""
    if not isinstance(message, dict):
        raise BridgeError("each message must be an object")
    role = message.get("role")
    content = message.get("content")
    if role not in ("user", "assistant"):
        raise BridgeError("message role must be user or assistant")
    if isinstance(content, str):
        return [{"role": role, "content": content}], set()
    if not isinstance(content, list):
        raise BridgeError("message content must be a string or a list")
    if role == "assistant":
        texts: list[str] = []
        calls: list[dict[str, object]] = []
        call_ids: set[str] = set()
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                texts.append(_require_str(block, "text"))
            elif kind == "tool_use":
                input_val = block.get("input")
                if input_val is not None and not isinstance(input_val, dict):
                    raise BridgeError("tool_use input must be a dict or None")
                call_id = _require_str(block, "id")
                # R18: Check for duplicate tool_use ids within this message
                if call_id in call_ids:
                    raise BridgeError(f"duplicate tool_use id {call_id!r} in assistant message")
                call_ids.add(call_id)
                calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": chat_name(_require_str(block, "name")),
                        "arguments": json.dumps(input_val or {}, separators=(",", ":")),
                    },
                })
            elif kind in ("thinking", "redacted_thinking"):
                continue
            else:
                raise BridgeError(f"unsupported assistant block: {kind}")
        text_content = "".join(texts) or None
        # Skip empty assistant messages that have no tool_calls
        if text_content is None and not calls:
            return [], set()
        translated: dict[str, object] = {"role": "assistant", "content": text_content}
        if calls:
            translated["tool_calls"] = calls
        return [translated], call_ids
    # User message handling
    if not content:
        raise BridgeError("user content list must not be empty")
    tool_messages: list[dict[str, object]] = []
    texts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": _require_str(block, "tool_use_id"),
                "content": _tool_result_text(block),
            })
        elif kind == "text":
            texts.append(_require_str(block, "text"))
        elif kind in ("image", "document"):
            raise BridgeError("image and document blocks are not supported")
        else:
            raise BridgeError(f"unsupported user block: {kind}")
    if texts:
        tool_messages.append({"role": "user", "content": "\n\n".join(texts)})
    return tool_messages, set()


def _translate_tool(tool: object, chat_name: Callable[[str], str]) -> dict[str, object]:
    if not isinstance(tool, dict) or "input_schema" not in tool:
        raise BridgeError("only custom tools with input_schema are supported")
    return {
        "type": "function",
        "function": {
            "name": chat_name(_require_str(tool, "name")),
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def _translate_tool_choice(choice: object, chat_name: Callable[[str], str]) -> object:
    if not isinstance(choice, dict):
        raise BridgeError("tool_choice must be an object")
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": chat_name(_require_str(choice, "name"))}}
    raise BridgeError(f"unsupported tool_choice: {kind}")


def anthropic_request_to_chat(request: Mapping[str, object], *, allowed_models: frozenset[str]) -> ChatTranslation:
    model = request.get("model")
    if not isinstance(model, str) or model not in allowed_models:
        raise BridgeError(f"model is not allowlisted: {model!r}")
    max_tokens = request.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise BridgeError("max_tokens must be a positive integer")
    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise BridgeError("messages must be a non-empty list")

    names: dict[str, str] = {}

    def chat_name(original: str) -> str:
        short = short_tool_name(original)
        if short in names and names[short] != original:
            raise BridgeError(f"tool name collision: {original!r} and {names[short]!r} both map to {short!r}")
        names[short] = original
        return short

    messages: list[dict[str, object]] = []
    system = _system_text(request.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    # Strict tool_call pairing (R17): pending_tool_ids is cleared and reset per assistant turn
    pending_tool_ids: set[str] = set()

    for i, message in enumerate(raw_messages):
        translated, call_ids = _translate_message(message, chat_name)

        # Handle assistant messages: update pending tool IDs
        if isinstance(message, dict) and message.get("role") == "assistant":
            # If this assistant message emits nothing (skipped thinking-only), leave pending unchanged
            if translated:  # Non-empty translation
                # If there are pending tool IDs and a new (non-empty) assistant message arrives, error
                if pending_tool_ids:
                    raise BridgeError(f"new assistant message arrived while tool_use_id {pending_tool_ids} still pending")

                # When an assistant message with tool_calls appears, it sets pending set
                if call_ids:
                    pending_tool_ids = call_ids

        # Handle user messages: validate tool result pairing
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")

            # Check if pending tool IDs exist with string-content user message
            if isinstance(content, str) and pending_tool_ids:
                raise BridgeError(f"string-content user message arrived while tool_use_id {pending_tool_ids} still pending")

            if isinstance(content, list):
                # Collect tool results in this user message
                seen_results: set[str] = set()
                has_tool_results = False
                has_text = False
                text_after_results = False

                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            has_tool_results = True
                            if has_text:
                                text_after_results = True
                            tool_use_id = block.get("tool_use_id")
                            if tool_use_id not in pending_tool_ids:
                                raise BridgeError(f"tool_result references unknown tool_use_id: {tool_use_id!r}")
                            if tool_use_id in seen_results:
                                raise BridgeError(f"tool_use_id {tool_use_id!r} answered multiple times in same user message")
                            seen_results.add(tool_use_id)
                        elif block.get("type") == "text":
                            has_text = True

                # If tool results are present, they must come before text
                if text_after_results:
                    raise BridgeError("tool_result blocks must appear before text blocks in user message")

                # If there are pending tool IDs but no results, error
                if pending_tool_ids and not has_tool_results:
                    raise BridgeError(f"tool_use_id {pending_tool_ids} still pending but user message has no tool results")

                # Verify all pending IDs are answered
                if has_tool_results and seen_results != pending_tool_ids:
                    missing = pending_tool_ids - seen_results
                    raise BridgeError(f"not all pending tool calls answered: {missing} missing")

                # Clear pending after processing user message
                pending_tool_ids.clear()

        messages.extend(translated)

    # After all messages, if pending tool IDs exist, error
    if pending_tool_ids:
        raise BridgeError(f"request ended with unanswered tool_use_id: {pending_tool_ids}")

    # After translating all messages, ensure there's at least one non-system message
    non_system_messages = [m for m in messages if m.get("role") != "system"]
    if not non_system_messages:
        raise BridgeError("messages must result in at least one non-system message")

    payload: dict[str, object] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    tools = request.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise BridgeError("tools must be a list")
        payload["tools"] = [_translate_tool(tool, chat_name) for tool in tools]
        if request.get("tool_choice") is not None:
            payload["tool_choice"] = _translate_tool_choice(request.get("tool_choice"), chat_name)
    else:
        # tool_choice without tools is only valid for auto/none
        tool_choice = request.get("tool_choice")
        if tool_choice is not None:
            if isinstance(tool_choice, dict):
                kind = tool_choice.get("type")
                if kind not in ("auto", "none"):
                    raise BridgeError("tool_choice requires tools unless type is auto or none")
            else:
                raise BridgeError("tool_choice must be an object")

    # Validate temperature
    if "temperature" in request:
        temp = request["temperature"]
        if isinstance(temp, bool):
            raise BridgeError("temperature must be a number, not a bool")
        if not isinstance(temp, (int, float)):
            raise BridgeError("temperature must be a number")
        payload["temperature"] = temp

    # Validate top_p
    if "top_p" in request:
        top_p = request["top_p"]
        if isinstance(top_p, bool):
            raise BridgeError("top_p must be a number, not a bool")
        if not isinstance(top_p, (int, float)):
            raise BridgeError("top_p must be a number")
        payload["top_p"] = top_p

    # Validate stop_sequences using `is not None` to catch falsy values
    stops = request.get("stop_sequences")
    if stops is not None:
        if not isinstance(stops, list):
            raise BridgeError("stop_sequences must be a list")
        for stop in stops:
            if not isinstance(stop, str) or not stop:
                raise BridgeError("stop_sequences must be a list of non-empty strings")
        payload["stop"] = list(stops)

    # Validate stream
    if "stream" in request:
        stream_val = request["stream"]
        if not isinstance(stream_val, bool):
            raise BridgeError("stream must be a bool")
        if stream_val is True:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

    return ChatTranslation(payload=payload, tool_names=dict(names))


STOP_REASONS: Mapping[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def to_sse(event: Mapping[str, object]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")


class ChatStreamToAnthropic:
    """Turn chat-completion stream chunks into Anthropic Messages stream events."""

    def __init__(self, *, model: str, tool_names: Mapping[str, str]) -> None:
        self._model = model
        self._names = dict(tool_names)
        self._message_id = "msg_" + uuid.uuid4().hex
        self._started = False
        self._finished = False
        self._next_index = 0
        self._open_index: int | None = None
        self._open_kind: str | None = None  # "text" or f"tool:{chat_index}"
        self._stop_reason = "end_turn"
        self.input_tokens = 0
        self.output_tokens = 0

    def _start(self) -> list[dict[str, object]]:
        if self._started:
            return []
        self._started = True
        return [{
            "type": "message_start",
            "message": {
                "id": self._message_id, "type": "message", "role": "assistant", "model": self._model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }]

    def _close(self) -> list[dict[str, object]]:
        if self._open_index is None:
            return []
        event: dict[str, object] = {"type": "content_block_stop", "index": self._open_index}
        self._open_index = None
        self._open_kind = None
        return [event]

    def _open(self, kind: str, block: dict[str, object]) -> list[dict[str, object]]:
        events = self._close()
        self._open_index = self._next_index
        self._open_kind = kind
        self._next_index += 1
        events.append({"type": "content_block_start", "index": self._open_index, "content_block": block})
        return events

    def feed(self, chunk: Mapping[str, object]) -> list[dict[str, object]]:
        if self._finished:
            raise UpstreamFormatError("stream chunk after finish")
        events = self._start()
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self.input_tokens = _count(usage.get("prompt_tokens"))
            self.output_tokens = _count(usage.get("completion_tokens"))
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise UpstreamFormatError("choices must be a list")
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise UpstreamFormatError("choice must be an object")
            delta = choice.get("delta") or {}
            if not isinstance(delta, Mapping):
                raise UpstreamFormatError("delta must be an object")
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self._open_kind != "text":
                    events += self._open("text", {"type": "text", "text": ""})
                events.append({"type": "content_block_delta", "index": self._open_index,
                               "delta": {"type": "text_delta", "text": text}})
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    raise UpstreamFormatError("tool call must be an object")
                function = call.get("function") or {}
                if not isinstance(function, Mapping):
                    raise UpstreamFormatError("tool call function must be an object")
                kind = f"tool:{call.get('index', 0)}"
                if self._open_kind != kind:
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        raise UpstreamFormatError("new tool call without a name")
                    events += self._open(kind, {
                        "type": "tool_use",
                        "id": call.get("id") or "toolu_" + uuid.uuid4().hex,
                        "name": self._names.get(name, name),
                        "input": {},
                    })
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    events.append({"type": "content_block_delta", "index": self._open_index,
                                   "delta": {"type": "input_json_delta", "partial_json": arguments}})
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                self._stop_reason = STOP_REASONS.get(reason, "end_turn")
        return events

    def finish(self) -> list[dict[str, object]]:
        if self._finished:
            return []
        events = self._start() + self._close()
        self._finished = True
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": self._stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
        })
        events.append({"type": "message_stop"})
        return events


def chat_completion_to_anthropic(
    response: Mapping[str, object], *, model: str, tool_names: Mapping[str, str]
) -> dict[str, object]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise UpstreamFormatError("response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        raise UpstreamFormatError("choice message must be an object")
    content: list[dict[str, object]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            raise UpstreamFormatError("tool call is malformed")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise UpstreamFormatError("tool call arguments are not JSON") from exc
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_" + uuid.uuid4().hex,
            "name": tool_names.get(function["name"], function["name"]),
            "input": arguments if isinstance(arguments, dict) else {"value": arguments},
        })
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    assert isinstance(usage, Mapping)
    reason = choice.get("finish_reason")
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": STOP_REASONS.get(reason, "end_turn") if isinstance(reason, str) else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": _count(usage.get("prompt_tokens")), "output_tokens": _count(usage.get("completion_tokens"))},
    }
