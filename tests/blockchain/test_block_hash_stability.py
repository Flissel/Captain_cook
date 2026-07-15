from blockchain.Blockchain_modell import Block


def test_hash_ignores_mutable_status_and_children() -> None:
    block = Block(
        index=1,
        block_type="work_batch",
        data={"batch_id": "batch-1"},
        status="pending",
        previous_hash="parent-hash",
        parent_index=0,
    )
    original_hash = block.hash

    block.status = "succeeded"
    block.children.append(2)

    assert block.compute_hash() == original_hash


def test_hash_is_stable_across_mapping_insertion_order() -> None:
    first = Block(1, "work_batch", {"a": 1, "b": 2}, "pending", "parent")
    second = Block(1, "work_batch", {"b": 2, "a": 1}, "pending", "parent")

    assert first.hash == second.hash
