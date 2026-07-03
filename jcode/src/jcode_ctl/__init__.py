"""jcode control server.

The box-side service behind JBrain's code mode (docs/archive/JCODE_PLAN.md,
Wave J1). It runs on the internal network only — like the supervisor and the
local-LLM gateway, it has no published host port — and exposes a small,
token-authed command set the JBrain api proxies (Wave J2): create/list a
sandboxed coding session in an isolated per-session git checkout, stream a turn,
cancel, reset, and delete.

It reads **no** knowledge base and holds **no** owner data — its only state is
the per-session workspaces under the sandbox volume. The coder model is on-box:
the Claude Agent SDK is pointed at the local gateway via ``ANTHROPIC_BASE_URL``,
so no code leaves the box.
"""
