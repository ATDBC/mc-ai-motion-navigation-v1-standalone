// Explicit pov_debug compatibility only; the formal scheduler never installs this file.
package com.kyhsgeekcode.minecraftenv

import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.Condition
import java.util.concurrent.locks.ReentrantLock

internal enum class ServerWorkKind {
    FREE,
    ARM,
    ACTION,
    RESET,
}

internal data class ActionPermit(val generation: Long)

internal data class ArmCompletion(val serverWorldTime: Long)

internal data class ActionCompletion(
    val generation: Long,
    val serverWorldTime: Long,
)

internal data class ServerWork(
    val kind: ServerWorkKind,
    val generation: Long,
) {
    companion object {
        fun free() = ServerWork(ServerWorkKind.FREE, 0L)

        fun arm() = ServerWork(ServerWorkKind.ARM, 0L)

        fun action(permit: ActionPermit) =
            ServerWork(ServerWorkKind.ACTION, permit.generation)

        fun reset() = ServerWork(ServerWorkKind.RESET, 0L)
    }
}

internal class TickSynchronizer {
    companion object {
        private val CLIENT_WAIT_NANOS = TimeUnit.SECONDS.toNanos(30)
        private val SERVER_WAIT_NANOS = TimeUnit.SECONDS.toNanos(30)
        private val SERVER_POLL_NANOS = TimeUnit.SECONDS.toNanos(1)
    }

    private val lock = ReentrantLock()
    private val serverWorkAvailable: Condition = lock.newCondition()
    private val armCompleted: Condition = lock.newCondition()
    private val actionCompleted: Condition = lock.newCondition()

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
    private var legacyPermit: ActionPermit? = null

    fun requestArmAndWait(): ArmCompletion {
        lock.lock()
        try {
            checkRunning()
            check(!armRequested && !armed) { "lockstep arm was requested twice" }
            armRequested = true
            serverWorkAvailable.signalAll()
            awaitClientCondition(armCompleted, "server arm") { armResult != null }
            return checkNotNull(armResult)
        } finally {
            lock.unlock()
        }
    }

    fun submitActionAndWait(): ActionCompletion {
        lock.lock()
        try {
            val permit = enqueueActionLocked()
            awaitClientCondition(actionCompleted, "action ${permit.generation}") {
                actionResult?.generation == permit.generation
            }
            return checkNotNull(actionResult).also { actionResult = null }
        } finally {
            lock.unlock()
        }
    }

    fun requestReset() {
        lock.lock()
        try {
            checkRunning()
            check(armed) { "lockstep reset requires an armed server" }
            check(pendingPermit == null && activePermit == null) {
                "cannot reset while an action generation is active"
            }
            check(!resetRequested) { "lockstep reset was requested twice" }
            resetRequested = true
            serverWorkAvailable.signalAll()
        } finally {
            lock.unlock()
        }
    }

    fun beginServerTick(): ServerWork {
        lock.lock()
        try {
            if (terminating) return ServerWork.free()
            if (resetRequested) return releaseForResetLocked()
            if (!armed) {
                if (armRequested && !armInFlight) {
                    armInFlight = true
                    return ServerWork.arm()
                }
                return ServerWork.free()
            }
            val deadline = System.nanoTime() + SERVER_WAIT_NANOS
            while (!terminating && !resetRequested && pendingPermit == null) {
                val remaining = deadline - System.nanoTime()
                if (remaining <= 0L) {
                    terminateLocked()
                    error("timed out waiting for the next action permit")
                }
                try {
                    serverWorkAvailable.awaitNanos(minOf(remaining, SERVER_POLL_NANOS))
                } catch (error: InterruptedException) {
                    Thread.currentThread().interrupt()
                    terminateLocked()
                    throw IllegalStateException(
                        "interrupted waiting for the next action permit",
                        error,
                    )
                }
            }
            if (terminating) return ServerWork.free()
            if (resetRequested) return releaseForResetLocked()
            val permit = checkNotNull(pendingPermit)
            check(activePermit == null) { "server already has active action work" }
            pendingPermit = null
            activePermit = permit
            return ServerWork.action(permit)
        } finally {
            lock.unlock()
        }
    }

    fun completeServerTick(
        work: ServerWork,
        serverWorldTime: Long,
    ) {
        lock.lock()
        try {
            when (work.kind) {
                ServerWorkKind.FREE -> Unit
                ServerWorkKind.RESET -> Unit
                ServerWorkKind.ARM -> {
                    check(armRequested && armInFlight && !armed) {
                        "invalid server arm completion"
                    }
                    armed = true
                    armInFlight = false
                    lastServerWorldTime = serverWorldTime
                    armResult = ArmCompletion(serverWorldTime)
                    armCompleted.signalAll()
                }
                ServerWorkKind.ACTION -> {
                    val permit = checkNotNull(activePermit) {
                        "server completed action without an active permit"
                    }
                    check(permit.generation == work.generation) {
                        "server action generation mismatch"
                    }
                    val previous = checkNotNull(lastServerWorldTime)
                    check(serverWorldTime == previous + 1L) {
                        "server world time advanced ${serverWorldTime - previous} ticks " +
                            "for action ${permit.generation}"
                    }
                    lastServerWorldTime = serverWorldTime
                    activePermit = null
                    actionResult = ActionCompletion(permit.generation, serverWorldTime)
                    actionCompleted.signalAll()
                }
            }
        } finally {
            lock.unlock()
        }
    }

    fun notifyServerTickStart() {
        lock.lock()
        try {
            if (!armed || terminating) return
            check(legacyPermit == null) { "legacy action permit is already pending" }
            legacyPermit = enqueueActionLocked()
        } finally {
            lock.unlock()
        }
    }

    fun waitForServerTickCompletion() {
        lock.lock()
        try {
            val permit = legacyPermit ?: return
            awaitClientCondition(actionCompleted, "legacy action ${permit.generation}") {
                actionResult?.generation == permit.generation
            }
            actionResult = null
            legacyPermit = null
        } finally {
            lock.unlock()
        }
    }

    fun waitForClientAction() = Unit

    fun notifyClientSendObservation() = Unit

    fun terminate() {
        lock.lock()
        try {
            terminateLocked()
        } finally {
            lock.unlock()
        }
    }

    private fun releaseForResetLocked(): ServerWork {
        check(resetRequested) { "lockstep reset release was not requested" }
        check(pendingPermit == null && activePermit == null) {
            "cannot release lockstep while an action generation is active"
        }
        resetRequested = false
        armRequested = false
        armInFlight = false
        armed = false
        armResult = null
        nextGeneration = 0L
        actionResult = null
        lastServerWorldTime = null
        legacyPermit = null
        return ServerWork.reset()
    }

    private fun terminateLocked() {
        terminating = true
        serverWorkAvailable.signalAll()
        armCompleted.signalAll()
        actionCompleted.signalAll()
    }

    private fun enqueueActionLocked(): ActionPermit {
        checkRunning()
        check(armed) { "lockstep server is not armed" }
        check(pendingPermit == null && activePermit == null) {
            "another action generation is still active"
        }
        nextGeneration += 1L
        return ActionPermit(nextGeneration).also {
            pendingPermit = it
            serverWorkAvailable.signalAll()
        }
    }

    private fun awaitClientCondition(
        condition: Condition,
        description: String,
        predicate: () -> Boolean,
    ) {
        var remaining = CLIENT_WAIT_NANOS
        while (!terminating && !predicate()) {
            check(remaining > 0L) { "timed out waiting for $description" }
            try {
                remaining = condition.awaitNanos(remaining)
            } catch (error: InterruptedException) {
                Thread.currentThread().interrupt()
                throw IllegalStateException("interrupted waiting for $description", error)
            }
        }
        checkRunning()
    }

    private fun checkRunning() {
        check(!terminating) { "lockstep synchronizer is terminating" }
    }
}
