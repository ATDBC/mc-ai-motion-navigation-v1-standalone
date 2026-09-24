package com.kyhsgeekcode.minecraftenv

import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

internal enum class ServerWorkKind { FREE, ARM, ACTION, RESET }
internal data class ActionPermit(val generation: Long)
internal data class ArmCompletion(val serverWorldTime: Long)
internal data class ActionCompletion(val generation: Long, val serverWorldTime: Long)
internal data class ServerWork(val kind: ServerWorkKind, val generation: Long) {
    companion object {
        fun free() = ServerWork(ServerWorkKind.FREE, 0L)
        fun arm() = ServerWork(ServerWorkKind.ARM, 0L)
        fun action(permit: ActionPermit) = ServerWork(ServerWorkKind.ACTION, permit.generation)
        fun reset() = ServerWork(ServerWorkKind.RESET, 0L)
    }
}

/** Training-only mailbox. No tick callback waits for the other thread or performs I/O here. */
internal class TickSynchronizer(private val clock: () -> Long = System::nanoTime) {
    private val lock = ReentrantLock()
    private var terminating = false
    private var armRequested = false
    private var armInFlight = false
    private var armed = false
    private var resetRequested = false
    private var armResult: ArmCompletion? = null
    private var nextGeneration = 0L
    private var pendingPermit: ActionPermit? = null
    private var activePermit: ActionPermit? = null
    private var actionResult: ActionCompletion? = null
    private var lastServerWorldTime: Long? = null
    private var deadline: Long? = null

    fun requestArm() = lock.withLock {
        checkRunning()
        check(!armRequested && !armed && !resetRequested) { "lockstep arm was requested twice or before reset release" }
        armRequested = true
        renewDeadline()
    }

    fun pollArmCompletion(): ArmCompletion? = lock.withLock {
        checkRunning()
        armResult?.also { armResult = null; renewDeadline() }
    }

    fun submitAction(): ActionPermit = lock.withLock {
        checkRunning()
        check(armed && !resetRequested) { "lockstep server is not armed or reset is pending" }
        check(pendingPermit == null && activePermit == null) { "another action generation is still active" }
        check(armResult == null && actionResult == null) { "lockstep completion is unconsumed" }
        check(nextGeneration < Long.MAX_VALUE) { "lockstep generation exhausted" }
        ActionPermit(++nextGeneration).also { pendingPermit = it; renewDeadline() }
    }

    fun pollActionCompletion(): ActionCompletion? = lock.withLock {
        checkRunning()
        actionResult?.also { actionResult = null; renewDeadline() }
    }

    fun requestReset() = lock.withLock {
        checkRunning()
        check(armed) { "lockstep reset requires an armed server" }
        check(pendingPermit == null && activePermit == null) { "cannot reset while an action generation is active" }
        check(armResult == null && actionResult == null) { "lockstep completion is unconsumed" }
        check(!resetRequested) { "lockstep reset was requested twice" }
        resetRequested = true
        renewDeadline()
    }

    fun beginServerTick(): ServerWork = lock.withLock {
        if (terminating) return ServerWork.free()
        checkRunning()
        check(!armInFlight && activePermit == null) { "server already has active work" }
        if (resetRequested) {
            resetRequested = false
            armRequested = false
            armed = false
            nextGeneration = 0L
            lastServerWorldTime = null
            deadline = null
            return ServerWork.reset()
        }
        if (!armed) {
            if (armRequested) { armInFlight = true; return ServerWork.arm() }
            return ServerWork.free()
        }
        val permit = pendingPermit ?: return ServerWork.free()
        pendingPermit = null
        activePermit = permit
        ServerWork.action(permit)
    }

    fun completeServerTick(work: ServerWork, serverWorldTime: Long) = lock.withLock {
        if (terminating) return
        checkRunning()
        when (work.kind) {
            ServerWorkKind.FREE, ServerWorkKind.RESET -> Unit
            ServerWorkKind.ARM -> {
                check(armRequested && armInFlight && !armed) { "invalid server arm completion" }
                armed = true
                armInFlight = false
                lastServerWorldTime = serverWorldTime
                armResult = ArmCompletion(serverWorldTime)
            }
            ServerWorkKind.ACTION -> {
                val permit = checkNotNull(activePermit) { "server completed action without an active permit" }
                check(permit.generation == work.generation) { "server action generation mismatch" }
                val previous = checkNotNull(lastServerWorldTime)
                check(serverWorldTime == previous + 1L) {
                    "server world time advanced ${serverWorldTime - previous} ticks for action ${permit.generation}"
                }
                lastServerWorldTime = serverWorldTime
                activePermit = null
                actionResult = ActionCompletion(permit.generation, serverWorldTime)
            }
        }
    }

    fun terminate() = lock.withLock { terminating = true }

    private fun renewDeadline() { deadline = clock() + 30_000_000_000L }
    private fun checkRunning() {
        check(!terminating) { "lockstep synchronizer is terminating" }
        if (deadline?.let { clock() - it >= 0L } == true) {
            terminating = true
            error("lockstep permit/completion timed out")
        }
    }
}
