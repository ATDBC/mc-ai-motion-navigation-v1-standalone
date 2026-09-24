"""Exact-source transformation for the approved formal reference scheduler.

Installation/fingerprinting is separate; transforming text alone is not native scheduling evidence.
"""
from __future__ import annotations

from pathlib import Path
import shutil

OVERLAY_ROOT = Path(__file__).resolve().parent / "runtime_overlays/mc121_transport"
NONBLOCKING_OUTPUTS = {
    "src/main/java/com/mc2p/transport/CraftGroundFrameTransport.java": OVERLAY_ROOT / "CraftGroundFrameTransport.java",
    "src/main/java/com/kyhsgeekcode/minecraftenv/NonBlockingMessageIO.kt": OVERLAY_ROOT / "NonBlockingMessageIO.kt",
    "src/main/java/com/kyhsgeekcode/minecraftenv/ClientEpisodeBinding.java": OVERLAY_ROOT / "ClientEpisodeBinding.java",
    "src/main/java/com/kyhsgeekcode/minecraftenv/ClientSessionDeadline.java": OVERLAY_ROOT / "ClientSessionDeadline.java",
}


def recipe_sources() -> tuple[Path, ...]:
    return (Path(__file__).resolve(), *NONBLOCKING_OUTPUTS.values())


def install_nonblocking_reference(sandbox: Path) -> tuple[str, ...]:
    target = sandbox / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
    transformed = patch_reference_source(target.read_text("utf-8"))
    for relative, source in NONBLOCKING_OUTPUTS.items():
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    target.write_text(transformed, encoding="utf-8")
    return ("install nonblocking reference scheduler v1",)


def _region(source: str, start: str, end: str, replacement: str) -> str:
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("nonblocking scheduler source anchor mismatch")
    first = source.index(start)
    last = source.index(end)
    if first >= last:
        raise ValueError("nonblocking scheduler source anchor order mismatch")
    return source[:first] + replacement + source[last:]


def patch_reference_source(source: str) -> str:
    if "mc2pNonblockingStopped" in source:
        raise ValueError("nonblocking scheduler is already installed")
    for anchor in ("serverSocket.accept()", "messageIO.readInitialEnvironment()", "messageIO.readAction()",
                   "messageIO.writeObservation(mc2pObservationSpaceMessage)", "tickSynchronizer.waitForClientAction()"):
        if anchor not in source:
            raise ValueError("nonblocking scheduler requires the structured reference source")
    source = _region(source, "    override fun onInitialize() {", "    private fun onStartWorldTick(", _INITIALIZE)
    source = _region(source, "    private fun onMc2pStartClientTick(", "    private fun sendSetScreenNull(", _ACTION)
    source = _region(source, '        } else if (command == "exit") {', "        } else {\n            runCommand(player, command)",
                     '        } else if (command == "exit") {\n            error("exit must use normal nonblocking shutdown")\n')
    # The outer END_CLIENT_TICK failure path owns cleanup, not global thread interruption or exitProcess.
    source = _region(source, "        } catch (e: IOException) {\n            e.printStackTrace()\n            tickSynchronizer.terminate()",
                     "    override fun runCommand(", "        } catch (e: IOException) {\n            throw e\n        }\n    }\n\n")
    source = source.replace("messageIO: MessageIO", "messageIO: NonBlockingMessageIO")
    reset_parser = 'command.substringAfter("fastreset ").trim()'
    if source.count(reset_parser) != 1:
        raise ValueError("nonblocking fastreset parser anchor mismatch")
    source = source.replace(reset_parser, 'command.substringAfter("fastreset ", "").trim()')
    writer = "messageIO.writeObservation(mc2pObservationSpaceMessage)"
    if source.count(writer) != 1:
        raise ValueError("nonblocking observation writer anchor mismatch")
    return source.replace(writer, "mc2pDeadline.check()\n            "
        "messageIO.offerObservation(mc2pEpoch.attach(mc2pObservationSpaceMessage))\n            "
        "mc2pDeadline.observationOffered()")


_INITIALIZE = '''    private var mc2pInitializer: EnvironmentInitializer? = null
    private val mc2pEpoch = ClientEpisodeBinding()
    private var mc2pEpisodeReady = false
    private var mc2pNonblockingStopped = false
    private lateinit var mc2pDeadline: ClientSessionDeadline
    private var mc2pEpisodeWorld: ClientWorld? = null
    private var mc2pEpisodePlayer: ClientPlayerEntity? = null

    override fun onInitialize() {
        check(StructuredObservationDiagnostics.STRUCTURED_ONLY)
        ClientTimeDiagnostics.initialize()
        val port = System.getenv("PORT")?.toInt() ?: 8000
        useSharedMemory = when (System.getenv("USE_SHARED_MEMORY")) {
            null, "0", "false" -> false
            "1", "true" -> true
            else -> error("invalid shared memory configuration")
        }
        val messageIO = NonBlockingMessageIO(port, useSharedMemory)
        mc2pDeadline = ClientSessionDeadline()
        messageIO.start()
        ClientTickEvents.START_CLIENT_TICK.register(ClientTickEvents.StartTick { client ->
            if (!mc2pNonblockingStopped) {
                try { onMc2pNonblockingStart(client, messageIO) }
                catch (error: Exception) { failMc2pNonblocking(client, messageIO, error) }
            }
        })
        ClientTickEvents.START_WORLD_TICK.register(ClientTickEvents.StartWorldTick { world ->
            val initializer = mc2pInitializer
            if (!mc2pNonblockingStopped && initializer != null) {
                try { onStartWorldTick(initializer, world, messageIO) }
                catch (error: Exception) { failMc2pNonblocking(MinecraftClient.getInstance(), messageIO, error) }
            }
        })
        ClientTickEvents.END_CLIENT_TICK.register(ClientTickEvents.EndTick { client ->
            if (!mc2pNonblockingStopped) {
                try { onMc2pNonblockingEnd(client, messageIO) }
                catch (error: Exception) { failMc2pNonblocking(client, messageIO, error) }
            }
        })
        net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents.CLIENT_STOPPING.register { client ->
            ClientTimeDiagnostics.closeAfter { stopMc2pNonblocking(client, messageIO) }
        }
    }

    private fun onMc2pNonblockingStart(client: MinecraftClient, messageIO: NonBlockingMessageIO) {
        messageIO.failure()?.let { throw IllegalStateException("transport worker failed", it) }
        mc2pDeadline.check()
        if (mc2pInitializer == null) {
            val initial = messageIO.pollInitialEnvironment() ?: return
            initialEnvironment = initial
            FramebufferCapturer.shouldCaptureDepth = initial.requiresDepth
            FramebufferCapturer.requiresDepthConversion = initial.requiresDepthConversion
            for (key in initial.blockCollisionKeysList) CollisionListener.blockCollisionInfoSet.add(key)
            for (key in initial.entityCollisionKeysList) CollisionListener.entityCollisionInfoSet.add(key)
            ioPhase = IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION
            resetPhase = ResetPhase.WAIT_INIT_ENDS
            ClientBehaviorCraftGroundBridge.beginFormalSession(client)
            mc2pInitializer = EnvironmentInitializer(initial, csvLogger)
        }
        mc2pInitializer!!.onClientTick(client)
        if (soundListener == null) soundListener = MinecraftSoundListener(client.soundManager)
        if (entityListener == null) entityListener = EntityRenderListenerImpl(client.worldRenderer as AddListenerInterface)
        if (deathMessageCollector == null) deathMessageCollector = client.networkHandler as GetMessagesInterface?
        if (mc2pEpisodeReady && (client.world !== mc2pEpisodeWorld || client.player !== mc2pEpisodePlayer ||
                client.player == null || client.player!!.isDead)) error("episode client context changed")
        if (!mc2pEpisodeReady || resetPhase != ResetPhase.END_RESET) {
            if (messageIO.pollAction() != null) error("request arrived during reset")
            return
        }
        ClientBehaviorCraftGroundBridge.tick(client)
        onMc2pStartClientTick(client, messageIO)
        if (mc2pEpisodeReady && resetPhase == ResetPhase.END_RESET && !mc2pNonblockingStopped)
            ClientBehaviorCraftGroundBridge.advance(client)
    }

    private fun onMc2pNonblockingEnd(client: MinecraftClient, messageIO: NonBlockingMessageIO) {
        mc2pDeadline.check()
        if (mc2pEpisodeReady && (client.world !== mc2pEpisodeWorld || client.player !== mc2pEpisodePlayer ||
                client.player == null || client.player!!.isDead)) error("episode client context changed")
        if (mc2pInitializer == null || resetPhase != ResetPhase.END_RESET) return
        val world = client.world ?: return
        val player = client.player ?: return
        if (player.isDead) error("cannot observe a dead reset player")
        if (ioPhase != IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION &&
            ioPhase != IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION) return
        if (!mc2pEpisodeReady) {
            mc2pEpoch.beginEpisode()
            mc2pEpisodeWorld = world
            mc2pEpisodePlayer = player
            mc2pEpisodeReady = true
        }
        sendObservation(messageIO, world)
    }

    private fun stopMc2pNonblocking(client: MinecraftClient, messageIO: NonBlockingMessageIO) {
        if (mc2pNonblockingStopped) return
        mc2pNonblockingStopped = true
        mc2pEpisodeReady = false
        mc2pEpisodeWorld = null
        mc2pEpisodePlayer = null
        mc2pEpoch.invalidate()
        mc2pInitializer = null
        variableCommandsAfterReset.clear()
        ioPhase = IOPhase.BEGINNING
        try { ClientBehaviorCraftGroundBridge.stop(client) }
        finally { messageIO.requestStop() }
    }

    private fun failMc2pNonblocking(client: MinecraftClient, messageIO: NonBlockingMessageIO, error: Exception) {
        System.err.println("MC2P_NONBLOCKING_FAILURE=" + error.javaClass.simpleName)
        error.printStackTrace()
        try { stopMc2pNonblocking(client, messageIO) }
        // Vanilla stop() owns world.disconnect -> client.disconnect -> save/close. Calling
        // client.disconnect here would wait for an integrated server not yet told to stop.
        finally { client.scheduleStop() }
    }

'''

_ACTION = '''    private fun onMc2pStartClientTick(client: MinecraftClient, messageIO: NonBlockingMessageIO) {
        val player = client.player ?: error("ready episode has no player")
        if (ioPhase != IOPhase.SENT_OBSERVATION_SHOULD_READ_ACTION &&
            ioPhase != IOPhase.GOT_INITIAL_ENVIRONMENT_SENT_OBSERVATION_SKIP_SEND_OBSERVATION) return
        val raw = messageIO.pollAction() ?: return
        val action = mc2pEpoch.unwrap(raw)
        val resetsEpisode = action.commandsList.any { it == "fastreset" || it == "fastreset " }
        if (resetsEpisode) mc2pDeadline.beginReset()
        ClientBehaviorCraftGroundBridge.validateBeforeDispatch(client, action)
        ioPhase = IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION
        if (resetsEpisode) {
            mc2pEpoch.invalidate()
            mc2pEpisodeReady = false
            mc2pEpisodeWorld = null
            mc2pEpisodePlayer = null
            mc2pObservationGeneration = -1L
        }
        for (command in action.commandsList) {
            if (command == "exit") {
                try { stopMc2pNonblocking(client, messageIO) }
                finally { client.scheduleStop() }
                return
            }
            if (handleCommand(command, client, player)) return
        }
        applyAction(action, player, client)
    }

'''
