"""Tests for the additive CaptainAgent <-> supply-chain-pipeline bridge
methods (agenten/Captain.py: build_default_supply_chain_pipeline,
submit_problem_to_supply_chain).

`agenten.Captain` imports the legacy `pyautogen` package (`import autogen`)
at module scope for its pre-existing, untouched methods -- that package is
deliberately NOT pinned in requirements.txt (see its own comment block),
so it is not installed in this repo's normal test environment today. Skip
cleanly rather than failing the whole suite if it's absent, the same way
tests/test_autogen_bus_integration.py skips cleanly without autogen_core.
"""
import pytest

pytest.importorskip("autogen")  # the legacy pyautogen 0.2 package agenten.Captain imports

from blockchain.Blockchain_modell import Blockchain  # noqa: E402
from blockchain.storage import InMemoryStorage  # noqa: E402

from agenten.Captain import CaptainAgent  # noqa: E402
from agenten.ledger_bridge.stage_machine import Stage  # noqa: E402

pytestmark = pytest.mark.asyncio


async def canned_llm_decompose(description, depth):
    if depth != 0:
        return []
    return [{"description": "A short atomic subproblem", "capability_tags": ["echo"], "atomic": True}]


async def canned_llm_judge(description, ruleset):
    return True


def make_captain(tmp_path) -> CaptainAgent:
    return CaptainAgent(name="captain", llm_config={}, blockchain_path=str(tmp_path / "captain_blockchain.json"))


async def test_submit_problem_to_supply_chain_without_pipeline_raises(tmp_path):
    captain = make_captain(tmp_path)
    with pytest.raises(RuntimeError):
        await captain.submit_problem_to_supply_chain("some problem description")


async def test_build_and_submit_reaches_done(tmp_path):
    captain = make_captain(tmp_path)
    pipeline = captain.build_default_supply_chain_pipeline(
        llm_decompose=canned_llm_decompose,
        llm_judge=canned_llm_judge,
        blockchain=Blockchain(storage=InMemoryStorage()),
    )
    assert captain.supply_chain_pipeline is pipeline

    await pipeline.start()
    problem_id = await captain.submit_problem_to_supply_chain("Problem Z: a captain-submitted problem")
    assert isinstance(problem_id, str) and problem_id

    converged = await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()

    assert converged
    assert pipeline.ledger_query.count_in_stage(Stage.DONE) == 1

    # An explicitly-passed blockchain is honored verbatim: the pipeline
    # uses it, not CaptainAgent's own self.blockchain.
    assert captain.blockchain is not pipeline.blockchain


async def test_default_pipeline_reuses_captains_own_blockchain(tmp_path):
    """Regression test for the silent-data-loss default: CaptainAgent's own
    ledger defaults to "blockchain.json" and build_pipeline's standalone
    default is the SAME file -- two independent Blockchain objects over one
    file full-overwrite each other on every save (JSONFileStorage.save
    rewrites the whole file), so whichever instance writes last silently
    erases the other's blocks. build_default_supply_chain_pipeline must
    therefore reuse self.blockchain when the caller doesn't name a ledger
    explicitly, making the two-instances-one-file collision impossible by
    default.
    """
    captain = make_captain(tmp_path)
    pipeline = captain.build_default_supply_chain_pipeline(
        llm_decompose=canned_llm_decompose,
        llm_judge=canned_llm_judge,
    )
    assert pipeline.blockchain is captain.blockchain

    # The shared instance really is live end-to-end: a problem submitted
    # through the supply chain lands blocks on the Captain's own ledger.
    await pipeline.start()
    await captain.submit_problem_to_supply_chain("Problem S: shared-ledger sanity check")
    assert await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()
    assert len(captain.blockchain.get_blocks_by_type("problem")) == 1
    assert len(captain.blockchain.get_blocks_by_type("subproblem")) == 1


async def test_submit_to_unstarted_pipeline_raises(tmp_path):
    """submit_problem_to_supply_chain on a built-but-never-started pipeline
    must raise (the Recorder's writer loop isn't draining -- the submission
    would silently go nowhere), per SupplyChainPipeline.submit_problem's
    started guard.
    """
    captain = make_captain(tmp_path)
    captain.build_default_supply_chain_pipeline(
        llm_decompose=canned_llm_decompose,
        llm_judge=canned_llm_judge,
        blockchain=Blockchain(storage=InMemoryStorage()),
    )
    with pytest.raises(RuntimeError, match="not started"):
        await captain.submit_problem_to_supply_chain("Problem T: submitted before start()")
