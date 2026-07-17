from gateway.app import canonical_captain_data_matches


def test_canonical_captain_data_matches_nested_content_independent_of_key_order() -> None:
    existing = {"batch_id": "batch-1", "schema": {"b": 2, "a": 1}}
    replay = {"schema": {"a": 1, "b": 2}, "batch_id": "batch-1"}

    assert canonical_captain_data_matches(existing, replay)


def test_canonical_captain_data_rejects_changed_content() -> None:
    existing = {"batch_id": "batch-1", "goal": "first"}
    replay = {"batch_id": "batch-1", "goal": "changed"}

    assert not canonical_captain_data_matches(existing, replay)
