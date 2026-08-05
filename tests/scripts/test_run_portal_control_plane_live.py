from scripts.run_portal_control_plane_live import (
    aggregate_requirements,
    cursor_path_for_run,
    provider_sequence,
    terminal_action_sequence,
)


def test_aggregate_run_requires_bearer_oauth_and_three_fixed_traces() -> None:
    releases = {"bearer": "a" * 64, "oauth2": "b" * 64}

    requirements = aggregate_requirements(releases)

    assert provider_sequence() == ("bearer", "oauth2", "bearer")
    assert terminal_action_sequence() == ("rotation_requested", "revoked")
    assert tuple(item.integration_key for item in requirements) == (
        "controlled_provider_bearer",
        "controlled_provider_oauth2",
    )
    assert tuple(item.credential_alias for item in requirements) == (
        "CONTROLLED_PROVIDER_BEARER",
        "CONTROLLED_PROVIDER_OAUTH",
    )
    assert tuple(item.credential_type for item in requirements) == (
        "httpBearerAuth",
        "oAuth2Api",
    )
    assert tuple(item.verification_workflow_sha256 for item in requirements) == (
        "a" * 64,
        "b" * 64,
    )


def test_full_rebuild_cursor_is_bound_to_the_immutable_run(tmp_path) -> None:
    first = cursor_path_for_run(tmp_path, "portal-aggregate-first")
    second = cursor_path_for_run(tmp_path, "portal-aggregate-second")

    assert first != second
    assert first.parent == tmp_path
    assert first.name == "minibook-portal-aggregate-first.db"
