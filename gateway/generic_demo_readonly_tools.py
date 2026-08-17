"""Deterministic read-only host tools for the repository-owned sales demo."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def execute_generic_tool(request: Mapping[str, object]) -> dict[str, object]:
    """Return public references or deterministic scores; never mutate state."""

    tool_key = request.get("tool_key")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    argument_sha256 = hashlib.sha256(canonical).hexdigest()
    if tool_key == "account_snapshot_reader":
        record_refs = arguments.get("record_refs", ())
        if not isinstance(record_refs, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in record_refs
        ):
            raise ValueError("account snapshot refs are invalid")
        return {
            "schema": "captain.sales-account-snapshot.v1",
            "argument_sha256": argument_sha256,
            "record_refs": sorted(set(record_refs)),
        }
    if tool_key == "opportunity_scoring_rules":
        components = arguments.get("components")
        if (
            not isinstance(components, dict)
            or any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 100
                for key, value in components.items()
            )
        ):
            raise ValueError("opportunity score components are invalid")
        return {
            "schema": "captain.sales-opportunity-score.v1",
            "argument_sha256": argument_sha256,
            "score": sum(components.values()),
            "component_keys": sorted(components),
        }
    if tool_key == "industry_signal_reader":
        signal_refs = arguments.get("signal_refs", ())
        if not isinstance(signal_refs, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in signal_refs
        ):
            raise ValueError("industry signal refs are invalid")
        return {
            "schema": "captain.sales-industry-signals.v1",
            "argument_sha256": argument_sha256,
            "signal_refs": sorted(set(signal_refs)),
        }
    raise ValueError("unknown repository-owned sales tool")


__all__ = ["execute_generic_tool"]
