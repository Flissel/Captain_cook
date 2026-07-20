from __future__ import annotations

import json

from agenten.agent_factory.live_demo_entrypoint import load_live_demo_release
from tests.integration.test_live_demo_runtime_chain import _release


def test_release_document_round_trips_into_factory_dispatch(tmp_path) -> None:
    expected = _release()
    document = {
        "dispatch": {
            "job": expected.dispatch.job.model_dump(mode="json", by_alias=True),
            "action": expected.dispatch.action.model_dump(mode="json"),
            "role": expected.dispatch.role.value if expected.dispatch.role else None,
            "lease": expected.dispatch.lease.model_dump(mode="json", by_alias=True)
            if expected.dispatch.lease
            else None,
        },
        "command": expected.command.model_dump(mode="json", by_alias=True),
    }
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(document), encoding="utf-8")

    observed = load_live_demo_release(release_path)

    assert observed == expected
