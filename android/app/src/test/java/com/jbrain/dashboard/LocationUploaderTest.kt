package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Test

private class InMemoryQueue : FixQueue {
    val items = ArrayDeque<LocationReport>()
    override fun enqueue(report: LocationReport) { items.addLast(report) }
    override fun peek(): LocationReport? = items.firstOrNull()
    override fun removeFirst() { items.removeFirstOrNull() }
    override fun size(): Int = items.size
    override fun clear() { items.clear() }
}

private class FakePublisher(var outcome: PublishOutcome = PublishOutcome.Published) : Publisher {
    val sent = mutableListOf<LocationReport>()
    override fun publish(serverBase: String, deviceKey: String, report: LocationReport): PublishOutcome {
        sent += report
        return outcome
    }
}

class LocationUploaderTest {
    private fun fix(tst: Long) = LocationReport(lat = 0.0, lon = 0.0, tst = tst)

    @Test
    fun submitSendsImmediatelyWhenOnline() {
        val q = InMemoryQueue()
        val pub = FakePublisher(PublishOutcome.Published)
        assertEquals(FlushResult.Drained, LocationUploader(q, pub).submit("s", "k", fix(7)))
        assertEquals(0, q.size())
        assertEquals(listOf(7L), pub.sent.map { it.tst })
    }

    @Test
    fun buffersThroughALapseAndBackfillsInOrderOnRecovery() {
        val q = InMemoryQueue()
        val pub = FakePublisher(PublishOutcome.Failed("offline"))
        val uploader = LocationUploader(q, pub)
        // Offline: both fixes are kept, not dropped.
        assertEquals(FlushResult.Paused, uploader.submit("s", "k", fix(1)))
        assertEquals(FlushResult.Paused, uploader.submit("s", "k", fix(2)))
        assertEquals(2, q.size())
        // Network returns → the backlog drains oldest-first.
        pub.sent.clear()
        pub.outcome = PublishOutcome.Published
        assertEquals(FlushResult.Drained, uploader.flush("s", "k"))
        assertEquals(0, q.size())
        assertEquals(listOf(1L, 2L), pub.sent.map { it.tst })
    }

    @Test
    fun rateLimitedPausesAndKeepsTheBacklog() {
        val q = InMemoryQueue()
        assertEquals(
            FlushResult.Paused,
            LocationUploader(q, FakePublisher(PublishOutcome.RateLimited)).submit("s", "k", fix(1)),
        )
        assertEquals(1, q.size())
    }

    @Test
    fun unauthorizedIsReportedAndLeavesTheQueueForTheCaller() {
        val q = InMemoryQueue()
        assertEquals(
            FlushResult.Unauthorized,
            LocationUploader(q, FakePublisher(PublishOutcome.Unauthorized)).submit("s", "k", fix(1)),
        )
        assertEquals(1, q.size()) // the caller clears creds + queue together
    }
}
