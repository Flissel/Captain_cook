from __future__ import annotations

from swarm.pipeline import SwarmPipeline


def test_noninteractive_pipeline_disables_all_user_feedback() -> None:
    pipeline = SwarmPipeline({}, "project-1", "Build a team", interactive=False)

    assert pipeline.allows_user_feedback() is False
