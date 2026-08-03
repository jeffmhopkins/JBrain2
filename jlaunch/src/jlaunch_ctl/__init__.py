"""jlaunch control server: run a registered long-running one-shot computation as a
supervised job with a live terminal, start/stop/kill, and a collectable artifact.

A sibling of the jcode control server (and the supervisor): an internal, token-authed
FastAPI service the JBrain api proxies to. Where jcode runs interactive coding sessions,
jlaunch runs a fixed "job spec" (clone -> phased shell pipeline -> artifact) to
completion, kept alive across browser disconnects and never idle-reaped while running.
"""
