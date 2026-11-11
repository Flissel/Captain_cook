"""Captain-authorized, cost-bounded OpenAI Responses proxy.

Legacy: this proxy meters Codex CLI on the paid OpenAI Responses API for the
live demo only. Captain's default provider is the Claude Code subscription
shim (``config.llm_provider``), so the proxy is retired from that path and gets
no new features; ``anthropic_bridge`` (the planned Claude Code to Nemotron
relay) will not be wired. Remove the package once the live demo no longer runs
Codex on OpenAI.
"""
