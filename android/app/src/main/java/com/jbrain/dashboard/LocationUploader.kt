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

/** Owns the offline fix queue. Fixes are enqueued first, then the queue is drained
 * oldest-first in BATCHES (one array POST per batch); the first transient failure
 * stops the drain and keeps the remainder, so a network lapse backfills in order
 * (each fix carries its real capture time) on the next flush or when connectivity
 * returns. Pure — unit-tested with a fake publisher. */
class LocationUploader(private val queue: FixQueue, private val publisher: Publisher) {
    /** Persist a fix without sending — the caller flushes on its own cadence
     * (every N points / T seconds) so a dense fix stream costs few requests. */
    fun enqueue(report: LocationReport) = queue.enqueue(report)

    /** How many fixes are waiting to be sent (drives the caller's flush trigger). */
    fun pending(): Int = queue.size()

    /** Persist this fix, then drain the queue immediately. */
    fun submit(serverBase: String, deviceKey: String, report: LocationReport): FlushResult {
        queue.enqueue(report)
        return flush(serverBase, deviceKey)
    }

    /** Drain the backlog oldest-first, up to MAX_BATCH per request, until empty or a
     * failure pauses it. A successful batch is dropped from the queue as a unit. */
    fun flush(serverBase: String, deviceKey: String): FlushResult {
        while (true) {
            val batch = queue.peekBatch(MAX_BATCH)
            if (batch.isEmpty()) return FlushResult.Drained
            when (publisher.publishBatch(serverBase, deviceKey, batch)) {
                PublishOutcome.Published -> queue.removeFirst(batch.size)
                PublishOutcome.Unauthorized -> return FlushResult.Unauthorized
                PublishOutcome.RateLimited -> return FlushResult.Paused
                is PublishOutcome.Failed -> return FlushResult.Paused
            }
        }
    }

    private companion object {
        // Matches the server's MAX_BATCH so a full backfill batch is never rejected
        // as over-large; a longer backlog drains as several batches.
        const val MAX_BATCH = 100
    }
}
