package com.jbrain.dashboard

import java.io.File

/** A persistent FIFO of pending location fixes, so a network lapse backfills instead
 * of dropping points. Bounded — the oldest are discarded when full. An interface so
 * the drain logic is unit-tested with an in-memory fake. */
interface FixQueue {
    fun enqueue(report: LocationReport)

    /** The oldest queued fix without removing it, or null when empty. */
    fun peek(): LocationReport?

    /** The up-to-[max] oldest fixes (oldest first) without removing them, for a
     * batched upload. Corrupt lines are skipped (and dropped) so they can't wedge. */
    fun peekBatch(max: Int): List<LocationReport>

    /** Drop the [count] oldest fixes (after they've been sent). */
    fun removeFirst(count: Int = 1)

    fun size(): Int

    fun clear()
}

/** Newline-delimited JSON on disk; survives process death. The whole (bounded) file
 * is rewritten on each mutation — cheap at this size and good enough for a
 * best-effort tracker. */
class FileFixQueue(private val file: File, private val capacity: Int = CAP) : FixQueue {
    private val lines = ArrayDeque<String>()

    init {
        if (file.exists()) file.readLines().forEach { if (it.isNotBlank()) lines.addLast(it) }
    }

    override fun enqueue(report: LocationReport) {
        lines.addLast(report.toJson())
        while (lines.size > capacity) lines.removeFirst() // drop the oldest when full
        persist()
    }

    override fun peek(): LocationReport? {
        while (lines.isNotEmpty()) {
            val parsed = LocationReport.fromJson(lines.first())
            if (parsed != null) return parsed
            lines.removeFirst() // skip a corrupt line so it can't wedge the queue
            persist()
        }
        return null
    }

    override fun peekBatch(max: Int): List<LocationReport> {
        if (max <= 0) return emptyList()
        val out = ArrayList<LocationReport>()
        var i = 0
        var dirty = false
        while (i < lines.size && out.size < max) {
            val parsed = LocationReport.fromJson(lines[i])
            if (parsed == null) {
                lines.removeAt(i) // drop a corrupt line; the next shifts into its place
                dirty = true
            } else {
                out.add(parsed)
                i++
            }
        }
        if (dirty) persist()
        return out
    }

    override fun removeFirst(count: Int) {
        var removed = 0
        while (removed < count && lines.isNotEmpty()) {
            lines.removeFirst()
            removed++
        }
        if (removed > 0) persist()
    }

    override fun size(): Int = lines.size

    override fun clear() {
        lines.clear()
        persist()
    }

    private fun persist() = file.writeText(lines.joinToString("\n"))

    private companion object {
        // ~7 h of dense moving fixes (5 s cadence) or weeks of stationary
        // heartbeats — the offline backfill buffer before the oldest are dropped.
        const val CAP = 5000
    }
}
