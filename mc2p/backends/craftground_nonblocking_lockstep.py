"""Training-only polling hooks layered over the formal, bounded reference transport/lifecycle."""
from pathlib import Path

from mc2p.backends.craftground_nonblocking import _region, install_nonblocking_reference


def _once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError("nonblocking lockstep hook anchor mismatch: " + old[:80])
    return source.replace(old, new, 1)


def install_nonblocking_lockstep(sandbox: Path) -> tuple[str, ...]:
    install_nonblocking_reference(sandbox)
    target = sandbox / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
    target.write_text(patch_lockstep_source(target.read_text("utf-8")), encoding="utf-8")
    return ("install nonblocking lockstep scheduler v1",)


def patch_lockstep_source(source: str) -> str:
    source = _once(source, "    private val tickSynchronizer = TickSynchronizer()\n", _FIELDS)
    source = _once(source, "        net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents.CLIENT_STOPPING.register { client ->",
        _SERVER + "        net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents.CLIENT_STOPPING.register { client ->")
    source = _once(source, "        messageIO.failure()?.let", "        LockstepClientSimulationGate.beginCycle(lockstepClientPhase == LockstepClientPhase.INITIALIZING)\n        messageIO.failure()?.let")
    # Enable the formal envelope before reset, but keep mining reference/deployment-only.
    source = _once(source, "ClientBehaviorCraftGroundBridge.beginFormalSession(client)",
                   "ClientBehaviorCraftGroundBridge.beginFormalSession(client, false)")
    source = _once(source, "        if (!mc2pEpisodeReady || resetPhase != ResetPhase.END_RESET) {",
                   "        ClientBehaviorCraftGroundBridge.tick(client)\n        if (!mc2pEpisodeReady || resetPhase != ResetPhase.END_RESET) {")
    source = _once(source, "        ClientBehaviorCraftGroundBridge.tick(client)\n        onMc2pStartClientTick(client, messageIO)",
                   "        onMc2pStartClientTick(client, messageIO)")
    source = _once(source, "        if (mc2pEpisodeReady && resetPhase == ResetPhase.END_RESET && !mc2pNonblockingStopped)",
                   "        if (lockstepActionPending && !mc2pNonblockingStopped)")
    source = _region(source, "    private fun onMc2pNonblockingEnd(", "    private fun stopMc2pNonblocking(", _END)
    source = _once(source, "        mc2pNonblockingStopped = true\n",
                   "        mc2pNonblockingStopped = true\n        tickSynchronizer.terminate()\n        LockstepClientSimulationGate.beginCycle(true)\n")
    source = _once(source, '        val player = client.player ?: error("ready episode has no player")',
                   '        if (lockstepClientPhase != LockstepClientPhase.READY) return\n        val player = client.player ?: error("ready episode has no player")')
    source = _once(source, "        val action = mc2pEpoch.unwrap(raw)\n",
                   "        val action = mc2pEpoch.unwrap(raw)\n"
                   '        require(action.commandsList.none { it == "respawn" }) { "lockstep respawn is unsupported; rebuild the episode" }\n')
    source = _once(source, "        if (resetsEpisode) {\n            mc2pEpoch.invalidate()",
                   "        if (resetsEpisode) {\n            beginLockstepReset()\n            mc2pEpoch.invalidate()")
    source = _once(source, "        applyAction(action, player, client)\n    }",
                   "        applyAction(action, player, client)\n        LockstepClientSimulationGate.admitAction()\n        lockstepActionPending = true\n    }")
    return source


_FIELDS = '''    private enum class LockstepClientPhase { INITIALIZING, ARM_WAIT, SETTLE, READY, SERVER_WAIT }
    private val tickSynchronizer = TickSynchronizer()
    private var lockstepServerWork = ServerWork.free()
    private var lockstepClientPhase = LockstepClientPhase.INITIALIZING
    private var lockstepActionPending = false
    private var lockstepGeneration = 0L
    private var lockstepServerWorldTime: Long? = null
    private var lockstepSettleTarget: Long? = null
    private var lockstepSettleStartedAtNanos: Long? = null
    private var lastEmittedClientWorldTime: Long? = null
    private val LOCKSTEP_SETTLE_TIMEOUT_NANOS = 10_000_000_000L
'''

_SERVER = '''        ServerTickEvents.END_SERVER_TICK.register(ServerTickEvents.EndTick { server ->
            try {
                val work = lockstepServerWork
                if (work.kind == ServerWorkKind.ARM || work.kind == ServerWorkKind.ACTION) {
                    LockstepTrace.record(
                        event = if (work.kind == ServerWorkKind.ARM) "server_arm_complete" else "server_tick_complete",
                        generation = work.generation, serverWorldTime = server.overworld.time)
                }
                tickSynchronizer.completeServerTick(work, server.overworld.time)
                val nextWork = tickSynchronizer.beginServerTick()
                when (nextWork.kind) {
                    ServerWorkKind.FREE -> Unit
                    ServerWorkKind.RESET -> server.tickManager.setFrozen(false)
                    ServerWorkKind.ARM -> {
                        server.tickManager.setFrozen(true)
                        server.sendTimeUpdatePackets()
                    }
                    ServerWorkKind.ACTION -> check(server.tickManager.step(1)) { "server refused action step" }
                }
                lockstepServerWork = nextWork
            } catch (error: Exception) {
                tickSynchronizer.terminate()
                MinecraftClient.getInstance().execute {
                    failMc2pNonblocking(MinecraftClient.getInstance(), messageIO, error)
                }
                throw error
            }
        })
'''

_END = '''    private fun onMc2pNonblockingEnd(client: MinecraftClient, messageIO: NonBlockingMessageIO) {
        mc2pDeadline.check()
        if (mc2pEpisodeReady && (client.world !== mc2pEpisodeWorld || client.player !== mc2pEpisodePlayer ||
                client.player == null || client.player!!.isDead)) error("episode client context changed")
        if (mc2pInitializer == null || resetPhase != ResetPhase.END_RESET) return
        val world = client.world ?: return
        val player = client.player ?: return
        check(!player.isDead) { "cannot observe a dead reset player" }
        when (lockstepClientPhase) {
            LockstepClientPhase.INITIALIZING -> {
                tickSynchronizer.requestArm()
                lockstepClientPhase = LockstepClientPhase.ARM_WAIT
                return
            }
            LockstepClientPhase.ARM_WAIT -> {
                val completion = tickSynchronizer.pollArmCompletion() ?: return
                lockstepGeneration = 0L
                lockstepServerWorldTime = completion.serverWorldTime
                lockstepSettleTarget = completion.serverWorldTime
                lockstepSettleStartedAtNanos = System.nanoTime()
                lockstepClientPhase = LockstepClientPhase.SETTLE
                return
            }
            LockstepClientPhase.READY -> {
                if (!lockstepActionPending) return
                lockstepGeneration = tickSynchronizer.submitAction().generation
                lockstepActionPending = false
                lockstepClientPhase = LockstepClientPhase.SERVER_WAIT
                return
            }
            LockstepClientPhase.SERVER_WAIT -> {
                val completion = tickSynchronizer.pollActionCompletion() ?: return
                check(completion.generation == lockstepGeneration) { "client action completion mismatch" }
                lockstepServerWorldTime = completion.serverWorldTime
                lockstepSettleTarget = checkNotNull(lastEmittedClientWorldTime) + 1L
                lockstepSettleStartedAtNanos = System.nanoTime()
                lockstepClientPhase = LockstepClientPhase.SETTLE
                return
            }
            LockstepClientPhase.SETTLE -> Unit
        }
        check(System.nanoTime() - checkNotNull(lockstepSettleStartedAtNanos) < LOCKSTEP_SETTLE_TIMEOUT_NANOS) {
            "lockstep client settle timed out"
        }
        val target = checkNotNull(lockstepSettleTarget)
        // A normal time packet can arrive before the permitted local tickTime and temporarily
        // overshoot. Wait for normal synchronization, never clamp/drop/delay a packet or simulate
        // another player tick. Native tickTime counts are independently checked by the probes.
        if (world.time != target) return
        if (!mc2pEpisodeReady) {
            mc2pEpoch.beginEpisode()
            mc2pEpisodeWorld = world
            mc2pEpisodePlayer = player
            mc2pEpisodeReady = true
        }
        LockstepTrace.record(event = "client_observation", generation = lockstepGeneration,
            serverWorldTime = lockstepServerWorldTime, clientWorldTime = world.time)
        sendObservation(messageIO, world)
        lastEmittedClientWorldTime = world.time
        lockstepSettleTarget = null
        lockstepSettleStartedAtNanos = null
        lockstepClientPhase = LockstepClientPhase.READY
    }

    private fun beginLockstepReset() {
        tickSynchronizer.requestReset()
        lockstepClientPhase = LockstepClientPhase.INITIALIZING
        lockstepActionPending = false
        lockstepGeneration = 0L
        lockstepServerWorldTime = null
        lockstepSettleTarget = null
        lockstepSettleStartedAtNanos = null
        lastEmittedClientWorldTime = null
        LockstepClientSimulationGate.beginCycle(true)
    }

'''
