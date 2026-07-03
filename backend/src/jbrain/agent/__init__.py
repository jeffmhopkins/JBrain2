"""The self-improving personal agent (docs/reference/ASSISTANT.md).

Built on the existing substrate — the LLM adapter, RLS-scoped sessions, the job
queue, storage — never around it. This package holds the agent loop, its tools,
memory, and the stage-and-approve plumbing; `contracts` defines the shared shapes
the loop, tool registry, chat stream, and PWA all agree on.
"""
