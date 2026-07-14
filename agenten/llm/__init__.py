"""Real (AutoGen-backed) LLM implementations for the injected callables used
by the event-driven supply-chain subsystem:

- `agenten.decomposition.decomposer.DecomposerAgent` takes `llm_decompose`.
- `agenten.constitution.gatekeeper.ConstitutionGatekeeper` takes `llm_judge`.

Both of those classes are pure/testable without any AutoGen import; this
package is where the actual `autogen_agentchat`/`autogen_ext` wiring lives,
kept separate so the core classes stay importable without AutoGen installed.
"""
