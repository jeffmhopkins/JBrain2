package com.jbrain.dashboard

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test

class FixQueueTest {
    private fun tempFile() = File.createTempFile("fixes", ".ndjson").apply { delete(); deleteOnExit() }
    private fun fix(tst: Long) = LocationReport(lat = tst.toDouble(), lon = 0.0, tst = tst)

    @Test
    fun persistsAcrossInstancesInFifoOrder() {
        val f = tempFile()
        FileFixQueue(f).apply {
            enqueue(fix(1))
            enqueue(fix(2))
        }
        // A fresh instance reads the file back — survives process death.
        val reloaded = FileFixQueue(f)
        assertEquals(2, reloaded.size())
        assertEquals(1L, reloaded.peek()?.tst) // oldest first
        reloaded.removeFirst()
        assertEquals(2L, reloaded.peek()?.tst)
    }

    @Test
    fun dropsTheOldestWhenOverCapacity() {
        val q = FileFixQueue(tempFile(), capacity = 2)
        q.enqueue(fix(1))
        q.enqueue(fix(2))
        q.enqueue(fix(3))
        assertEquals(2, q.size())
        assertEquals(2L, q.peek()?.tst) // the oldest (1) was discarded
    }

    @Test
    fun clearEmptiesAndPersists() {
        val f = tempFile()
        FileFixQueue(f).apply {
            enqueue(fix(1))
            clear()
        }
        assertEquals(0, FileFixQueue(f).size())
    }

    @Test
    fun skipsACorruptHeadLineSoItCannotWedge() {
        val f = tempFile()
        f.writeText("garbage\n" + fix(5).toJson())
        assertEquals(5L, FileFixQueue(f).peek()?.tst)
    }
}
