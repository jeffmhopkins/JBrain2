package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Test

private class InMemoryQueue : FixQueue {
    val items = ArrayDeque<LocationReport>()
    override fun enqueue(report: LocationReport) { items.addLast(report) }
    override fun peek(): LocationReport? = items.firstOrNull()
    override fun peekBatch(max: Int): List<LocationReport> = items.take(max)
    override fun removeFirst(count: Int) { repeat(count) { items.removeFirstOrNull() } }
    override fun size(): Int = items.size
    override fun clear() { items.clear() }
}

private class FakePublisher(var outcome: PublishOutcome = PublishOutcome.Published) : Publisher {
    val sent = mutableListOf<LocationReport>()
    val batchSizes = mutableListOf<Int>()
    override fun publishBatch(
        serverBase: String,
        deviceKey: String,
        reports: List<LocationReport>,
    ): PublishOutcome {
        sent += reports
        batchSizes += reports.size
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

    @Test
    fun enqueueDefersAndPendingCountsTheBacklog() {
        val q = InMemoryQueue()
        val pub = FakePublisher()
        val uploader = LocationUploader(q, pub)
        uploader.enqueue(fix(1))
        uploader.enqueue(fix(2))
        // enqueue does not send — the caller flushes on its own cadence.
        assertEquals(2, uploader.pending())
        assertEquals(0, pub.sent.size)
        assertEquals(FlushResult.Drained, uploader.flush("s", "k"))
        assertEquals(0, uploader.pending())
        assertEquals(listOf(1L, 2L), pub.sent.map { it.tst })
    }

    @Test
    fun flushDrainsInOneBatchPerRequest() {
        val q = InMemoryQueue()
        val pub = FakePublisher()
        val uploader = LocationUploader(q, pub)
        for (t in 1..5L) uploader.enqueue(fix(t))
        assertEquals(FlushResult.Drained, uploader.flush("s", "k"))
        // All five went up in a single batch POST (not five requests).
        assertEquals(listOf(5), pub.batchSizes)
        assertEquals(listOf(1L, 2L, 3L, 4L, 5L), pub.sent.map { it.tst })
        assertEquals(0, q.size())
    }
}
