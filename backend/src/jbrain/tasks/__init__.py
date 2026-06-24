"""Tasks: saved prompts that spawn an agent session on a schedule or on demand.

The owner authors a task (prompt + persona + schedule); the scheduler fires due
tasks, the runner executes one agent turn under the task's persona/scope and
records the run + the session it produced, and the run history links back to those
historical sessions. See docs/mocks/tasks-launcher-README.md for the surface.
"""
