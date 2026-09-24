package com.kyhsgeekcode.minecraftenv

import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2
import com.kyhsgeekcode.minecraftenv.proto.InitialEnvironment.InitialEnvironmentMessage
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace.ObservationSpaceMessage
import com.mc2p.transport.CraftGroundFrameTransport

/** Typed client-thread boundary; a missing complete frame is normal, never a socket wait. */
class NonBlockingMessageIO(
    port: Int,
    useSharedMemory: Boolean,
    bootstrapTimeoutMillis: Long = 90_000,
    frameTimeoutMillis: Long = 30_000,
) : AutoCloseable {
    private val owner = Thread.currentThread()
    private val frames: CraftGroundFrameTransport
    private var initialized = false
    @Volatile private var stopped = false
    @Volatile private var terminalFailure: Throwable? = null

    init {
        require(!useSharedMemory) { "formal nonblocking transport does not support shared memory" }
        frames = CraftGroundFrameTransport(port, bootstrapTimeoutMillis, frameTimeoutMillis)
    }

    fun start() = boundary { frames.start() }

    fun pollInitialEnvironment(): InitialEnvironmentMessage? = boundary {
        check(!initialized) { "initial environment was already consumed" }
        val bytes = frames.poll() ?: return@boundary null
        InitialEnvironmentMessage.parseFrom(bytes).also { initialized = true }
    }

    fun pollAction(): ActionSpaceMessageV2? = boundary {
        check(initialized) { "action before initial environment" }
        val bytes = frames.poll() ?: return@boundary null
        ActionSpaceMessageV2.parseFrom(bytes)
    }

    fun offerObservation(observation: ObservationSpaceMessage) = boundary {
        check(initialized) { "observation before initial environment" }
        frames.offer(observation.toByteArray())
    }

    fun failure(): Throwable? = terminalFailure ?: frames.failure()
    fun isListening(): Boolean = frames.isListening
    fun isStopped(): Boolean = frames.isStopped

    /** Safe at a client tick: no join and no socket ownership transfer. */
    fun requestStop() {
        stopped = true
        frames.requestStop()
    }

    @Synchronized private fun fail(error: Throwable) {
        if (terminalFailure == null) terminalFailure = frames.failure() ?: error
        requestStop()
    }

    private inline fun <T> boundary(operation: () -> T): T {
        if (stopped) throw IllegalStateException("message pump stopped", failure())
        try {
            check(Thread.currentThread() === owner) { "message pump called outside owner thread" }
            return operation()
        } catch (error: Exception) {
            fail(error)
            throw IllegalStateException("message pump failed", error)
        }
    }

    /** Owner teardown only, not a client-tick operation; the worker join is bounded. */
    override fun close() {
        requestStop()
        try {
            frames.close()
        } catch (error: Exception) {
            fail(error)
            throw error
        }
    }
}
