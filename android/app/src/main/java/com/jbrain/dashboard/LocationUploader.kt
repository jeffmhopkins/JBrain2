package com.jbrain.dashboard

/** The result of a queue flush. */
sealed interface FlushResult {
    /** The queue is empty — everything pending was sent. */
    data object Drained : FlushResult

    /** Offline / transient / rate-limited: the backlog is kept for a later retry. */
    data object Paused : FlushResult

    /** The key was revoked; the caller should clear the credentials and the queue. */
    data object Unauthorized : FlushResult
}

/** Owns the offline fix queue. Every fix is enqueued first, then the queue is drained
 * oldest-first; the first transient failure stops the drain and keeps the remainder,
 * so a network lapse backfills in order (each fix carries its real capture time) on
 * the next fix or when connectivity returns. Pure — unit-tested with a fake publisher. */
class LocationUploader(private val queue: FixQueue, private val publisher: Publisher) {
    /** Persist this fix, then try to drain the queue. */
    fun submit(serverBase: String, deviceKey: String, report: LocationReport): FlushResult {
        queue.enqueue(report)
        return flush(serverBase, deviceKey)
    }

    /** Drain the backlog oldest-first until empty or a failure pauses it. */
    fun flush(serverBase: String, deviceKey: String): FlushResult {
        while (true) {
            val report = queue.peek() ?: return FlushResult.Drained
            when (publisher.publish(serverBase, deviceKey, report)) {
                PublishOutcome.Published -> queue.removeFirst()
                PublishOutcome.Unauthorized -> return FlushResult.Unauthorized
                PublishOutcome.RateLimited -> return FlushResult.Paused
                is PublishOutcome.Failed -> return FlushResult.Paused
            }
        }
    }
}
