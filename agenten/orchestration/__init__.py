"""Final integration point (unit U11) for the event-driven supply-chain
pipeline: wires every already-merged unit's agents onto a shared EventBus
and returns a handle a caller (CaptainAgent, examples/armada_demo.py,
tests) can submit problems through.

See agenten/orchestration/pipeline.py for build_pipeline().
"""
