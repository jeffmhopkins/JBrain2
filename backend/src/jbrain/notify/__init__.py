"""Self-hosted owner notifications: an in-process fan-out the native owner app consumes
over an authenticated SSE stream and renders as local device notifications. Server-side
subsystems (task-ready, ...) publish here; the delivery never leaves the owner's own
server (no third party, no FCM), so events carry their content directly."""

from jbrain.notify.bus import Notification, NotifyBus, notify_owner

__all__ = ["Notification", "NotifyBus", "notify_owner"]
