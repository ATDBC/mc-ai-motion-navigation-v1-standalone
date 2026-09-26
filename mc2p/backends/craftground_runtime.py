"""Prepare fingerprinted, isolated CraftGround mc121 runtime sandboxes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import importlib.metadata
import importlib.resources
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Callable, Iterable, Mapping
import uuid

from mc2p.backends.craftground_nonblocking import NONBLOCKING_OUTPUTS, install_nonblocking_reference, recipe_sources
from mc2p.backends.craftground_nonblocking_lockstep import install_nonblocking_lockstep
from mc2p.contracts.observation_request_v3 import OBSERVATION_V2, OBSERVATION_V3


SANDBOX_SCHEMA_VERSION = "mc2p.craftground-sandbox.v3"
SANDBOX_MANIFEST_NAME = "sandbox-manifest.json"
MIXIN_RELATIVE_PATH = (
    "src/main/resources/com.kyhsgeekcode.minecraftenv.mixin.json"
)
LOCKSTEP_CONTROLLER_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/TickSynchronizer.kt"
)
MINECRAFT_ENV_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
)
LOCKSTEP_TRACE_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/LockstepTrace.kt"
)
FRAMEBUFFER_CAPTURER_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/FramebufferCapturer.kt"
)
RENDER_MIXIN_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/RenderMixin.java"
)
GAME_RENDERER_MIXIN_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/GameRendererMixin.java"
)
WINDOW_OFFSCREEN_MIXIN_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/WindowOffScreenMixin.java"
)
STRUCTURED_DIAGNOSTICS_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/StructuredObservationDiagnostics.kt"
)
CLIENT_OBSERVATION_COLLECTOR_RELATIVE_PATH = (
    "src/main/java/com/mc2p/observation/ClientObservationCollector.java"
)
CLIENT_OBSERVATION_JSON_RELATIVE_PATH = (
    "src/main/java/com/mc2p/observation/ClientObservationJson.java"
)
CLIENT_GUI_SESSION_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientGuiSession.java"
CLIENT_SAMPLE_CLOCK_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientSampleClock.java"
CLIENT_ENTITY_NAME_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientEntityName.java"
CLIENT_ENTITY_INDEX_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientEntityIndex.java"
CLIENT_DAMAGE_EVENT_BUFFER_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientDamageEventBuffer.java"
CLIENT_CROSSHAIR_ACCESS_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientCrosshairAccess.java"
CLIENT_BLOCK_OBSERVATION_V3_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientBlockObservationV3.java"
CLIENT_BLOCK_PARITY_DIAGNOSTICS_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientBlockParityDiagnostics.java"
CLIENT_OBSERVATION_REQUEST_V3_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientObservationRequestV3.java"
CLIENT_GUI_PROPERTIES_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientGuiProperties.java"
CLIENT_GUI_PROPERTY_ACCESS_RELATIVE_PATH = "src/main/java/com/mc2p/observation/ClientGuiPropertyAccess.java"
CLIENT_OBSERVATION_BRIDGE_RELATIVE_PATH = (
    "src/main/java/com/kyhsgeekcode/minecraftenv/ClientObservationCraftGroundBridge.java"
)
LOCKSTEP_OVERLAY_ROOT = (
    Path(__file__).resolve().parent / "runtime_overlays" / "mc121_lockstep"
)
LOCKSTEP_PLAYER_GATE_RELATIVE_PATH = "src/main/java/com/kyhsgeekcode/minecraftenv/LockstepClientSimulationGate.java"
LOCKSTEP_PLAYER_MIXIN_RELATIVE_PATH = "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/LockstepPlayerTickMixin.java"
STRUCTURED_OVERLAY_ROOT = (
    Path(__file__).resolve().parent / "runtime_overlays" / "mc121_structured"
)
OBSERVATION_OVERLAY_ROOT = (
    Path(__file__).resolve().parent / "runtime_overlays" / "mc121_observation"
)
ACTION_OVERLAY_ROOT = Path(__file__).resolve().parent / "runtime_overlays" / "mc121_actions"
DIAGNOSTIC_OVERLAY_ROOT = Path(__file__).resolve().parent / "runtime_overlays" / "mc121_diagnostics"
BEHAVIOR_OVERLAYS = {
    **{f"src/main/java/com/mc2p/actions/{name}": ACTION_OVERLAY_ROOT / name for name in (
        "ClientRequestGate.java", "ClientActionRequest.java", "ClientBehaviorExecutor.java", "ClientSlotGuard.java",
        "ClientBehaviorInput.java", "ClientBehaviorHooks.java",
        "ClientBlockGuard.java", "ClientBehaviorAccess.java", "ClientUsePulse.java",
        "ClientMiningLoop.java", "ClientMiningDriver.java",
    )},
    "src/main/java/com/kyhsgeekcode/minecraftenv/ClientBehaviorCraftGroundBridge.java": (
        STRUCTURED_OVERLAY_ROOT / "ClientBehaviorCraftGroundBridge.java"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/ClientBehaviorEnvelope.java": (
        STRUCTURED_OVERLAY_ROOT / "ClientBehaviorEnvelope.java"
    ),
    **{f"src/main/java/com/mc2p/diagnostics/{name}": DIAGNOSTIC_OVERLAY_ROOT / name
       for name in ("ClientTimeTrace.java", "ClientTimeDiagnostics.java", "ClientTimeSegmentWriter.java",
                    "ClientMovementDiagnostics.java", "ClientControlDiagnostics.java",
                    "ClientPhysicsTickDiagnostics.java")},
    **{f"src/main/java/com/kyhsgeekcode/minecraftenv/mixin/{name}": STRUCTURED_OVERLAY_ROOT / name
       for name in ("BehaviorKeyboardMixin.java", "BehaviorMouseMixin.java", "HandledScreenRenderMixin.java",
                    "BehaviorPlayerMixin.java", "BehaviorInputMixin.java", "ScreenHandlerPropertiesMixin.java",
                    "ClientClockTickMixin.java", "ClientClockWorldMixin.java", "ClientClockPacketMixin.java")},
}
MC121_RUNTIME_0_1_0_FINGERPRINTS: Mapping[str, str] = {
    MINECRAFT_ENV_RELATIVE_PATH: (
        "e96a2853f87196f7c38a74a1d4823764e66d178dfe143cd8291a16503961ac48"
    ),
    LOCKSTEP_CONTROLLER_RELATIVE_PATH: (
        "00df5cbf67d8221d4506bd404b00794587f12f6b9bf8d489b30830ccd69316be"
    ),
    FRAMEBUFFER_CAPTURER_RELATIVE_PATH: (
        "bf2696fd179e55e5e1b17d444a0c385aee19cccfe15a5a05b581902adf046720"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/EnvironmentInitializer.kt": (
        "475d46a165d2a56695eb0040c90cb525ef66d71b4fb2215a5a4da0c0268e1a37"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/TickSpeed.kt": (
        "d67f7b0f2d664d787838a2a5f6423c4d6684f33b05e4c64a55b2f67bc2c7f3ef"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/TickSpeedMixin.java": (
        "d6b62fdeaa48bd90cf7053d966692c9561a9ef18268cedef7fc5c26544ae9f77"
    ),
    RENDER_MIXIN_RELATIVE_PATH: (
        "0e1bc7acec0086fd74eee3d175854403b4f645cf5f1238ea8ca4bcbb2c19f3e1"
    ),
    WINDOW_OFFSCREEN_MIXIN_RELATIVE_PATH: (
        "5a9b60849e2cb92ff7bfcf226b8f9c21e59a123a9616f502fc7f593e0adff4bc"
    ),
    MIXIN_RELATIVE_PATH: (
        "e109d2f081b885cdf1c5605c36c3bc44205dec5cd39e94a8dfb834697139f08d"
    ),
    "build.gradle": (
        "61a1709079c2429cf095ad5c850e55009606feb6a646f72af027e335687f17dc"
    ),
}

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        "run",
        ".gradle",
        ".kotlin",
        "build",
        "__pycache__",
        "_deps",
        "cmakefiles",
        "debug",
        "release",
        "x64",
    }
)
_IGNORED_GENERATED_FILE_NAMES = frozenset(
    {"cmakecache.txt", "cmake_install.cmake"}
)
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CraftGroundClockModeV0(StrEnum):
    ACCELERATED = "accelerated"
    REFERENCE_20_TPS = "reference_20_tps"
    LOCKSTEP_ACCELERATED = "lockstep_accelerated"


class CraftGroundObservationModeV0(StrEnum):
    STRUCTURED_ONLY = "structured_only"
    POV_DEBUG = "pov_debug"


class RuntimePreparationError(RuntimeError):
    """Raised when a runtime source or sandbox cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CraftGroundSandboxManifestV0:
    schema_version: str
    craftground_version: str
    runtime_version: str
    clock_mode: CraftGroundClockModeV0
    observation_mode: CraftGroundObservationModeV0
    source_root: str
    sandbox_root: str
    source_fingerprints: tuple[tuple[str, str], ...]
    output_fingerprints: tuple[tuple[str, str], ...]
    patch_recipe_fingerprint: str
    patch_operations: tuple[str, ...]
    created_at_utc: str
    observation_schema_version: str = OBSERVATION_V2


@dataclass(frozen=True, slots=True)
class PreparedCraftGroundRuntimeV0:
    path: Path
    manifest: CraftGroundSandboxManifestV0
    reused: bool


def resolve_mc121_runtime_path() -> Path:
    return Path(str(importlib.resources.files("craftground_runtime_mc121"))).resolve()


def _normal_relative_path(value: str) -> str:
    candidate = Path(value.replace("/", os.sep))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RuntimePreparationError(f"unsafe runtime relative path: {value!r}")
    return candidate.as_posix()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_runtime_source_fingerprints(
    source_root: Path,
    relative_paths: Iterable[str] = MC121_RUNTIME_0_1_0_FINGERPRINTS,
) -> dict[str, str]:
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise RuntimePreparationError(f"runtime source is not a directory: {root}")
    result: dict[str, str] = {}
    for raw_relative in relative_paths:
        relative = _normal_relative_path(str(raw_relative))
        target = root / Path(relative)
        if not target.is_file():
            raise RuntimePreparationError(f"runtime source file is missing: {relative}")
        result[relative] = _hash_file(target)
    return result


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimePreparationError(
            f"required distribution is unavailable: {distribution}"
        ) from error


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name.casefold() in _IGNORED_DIRECTORY_NAMES
        or name.casefold() in _IGNORED_GENERATED_FILE_NAMES
        or name.casefold().endswith(
            (".log", ".dir", ".vcxproj", ".vcxproj.filters", ".sln")
        )
    }


def _patch_reference_mixin(sandbox_root: Path) -> tuple[str, ...]:
    mixin_path = sandbox_root / Path(MIXIN_RELATIVE_PATH)
    try:
        value = json.loads(mixin_path.read_text(encoding="utf-8"))
        clients = value["client"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimePreparationError(f"invalid mixin manifest: {error}") from error
    if not isinstance(clients, list) or clients.count("TickSpeedMixin") != 1:
        raise RuntimePreparationError(
            "mixin manifest must register TickSpeedMixin exactly once"
        )
    value["client"] = [item for item in clients if item != "TickSpeedMixin"]
    mixin_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return ("remove client mixin TickSpeedMixin",)


def _install_lockstep_overlays(sandbox_root: Path, *, formal: bool) -> None:
    overlays = {
        LOCKSTEP_CONTROLLER_RELATIVE_PATH: LOCKSTEP_OVERLAY_ROOT / (
            "TickSynchronizer.kt" if formal else "LegacyTickSynchronizer.kt"),
        LOCKSTEP_TRACE_RELATIVE_PATH: LOCKSTEP_OVERLAY_ROOT / "LockstepTrace.kt",
        LOCKSTEP_PLAYER_GATE_RELATIVE_PATH: LOCKSTEP_OVERLAY_ROOT / "LockstepClientSimulationGate.java",
        LOCKSTEP_PLAYER_MIXIN_RELATIVE_PATH: LOCKSTEP_OVERLAY_ROOT / "LockstepPlayerTickMixin.java",
    }
    for relative, source in overlays.items():
        if not source.is_file():
            raise RuntimePreparationError(f"lockstep overlay is missing: {source}")
        destination = sandbox_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    try:
        mixin_path = sandbox_root / Path(MIXIN_RELATIVE_PATH)
        mixins = json.loads(mixin_path.read_text(encoding="utf-8"))
        if "LockstepPlayerTickMixin" in mixins["client"]:
            raise RuntimePreparationError("duplicate lockstep player tick mixin")
        mixins["client"].append("LockstepPlayerTickMixin")
        mixin_path.write_text(json.dumps(mixins, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise RuntimePreparationError(
            f"cannot read lockstep hook target: {error}"
        ) from error


def _patch_lockstep_runtime(sandbox_root: Path) -> tuple[str, ...]:
    """Explicit historical pov_debug compatibility; not installed in the formal scheduler."""
    _install_lockstep_overlays(sandbox_root, formal=False)
    minecraft_env_path = sandbox_root / Path(MINECRAFT_ENV_RELATIVE_PATH)
    minecraft_env = minecraft_env_path.read_text(encoding="utf-8")
    field_anchor = "    private val tickSynchronizer = TickSynchronizer()\n"
    if minecraft_env.count(field_anchor) != 1:
        raise RuntimePreparationError(
            "lockstep field hook anchor must occur exactly once"
        )
    minecraft_env = minecraft_env.replace(
        field_anchor,
        """    private enum class LockstepClientPhase {
        INITIALIZING,
        ACTION,
        SETTLE,
    }

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
""",
        1,
    )

    client_start_tail = """                csvLogger.profileEndPrint("Minecraft_env/onInitialize/ClientTick")
            },
        )
        ClientTickEvents.START_WORLD_TICK.register(
"""
    if minecraft_env.count(client_start_tail) != 1:
        raise RuntimePreparationError(
            "lockstep client-start hook anchor must occur exactly once"
        )
    minecraft_env = minecraft_env.replace(
        client_start_tail,
        """                onLockstepStartClientTick(client, messageIO)
                csvLogger.profileEndPrint("Minecraft_env/onInitialize/ClientTick")
            },
        )
        ClientTickEvents.START_WORLD_TICK.register(
""",
        1,
    )

    client_end = "        ClientTickEvents.END_WORLD_TICK.register("
    if minecraft_env.count(client_end) != 1:
        raise RuntimePreparationError(
            "lockstep client-end hook anchor must occur exactly once"
        )
    client_end_index = minecraft_env.index(client_end)
    server_start_index = minecraft_env.index(
        "        ServerTickEvents.START_SERVER_TICK.register(",
        client_end_index,
    )
    client_end_hook = """        ClientTickEvents.END_WORLD_TICK.register(
            ClientTickEvents.EndWorldTick { world: ClientWorld ->
                onLockstepEndWorldTick(messageIO, world)
            },
        )
"""
    minecraft_env = (
        minecraft_env[:client_end_index]
        + client_end_hook
        + minecraft_env[server_start_index:]
    )

    server_start = "        ServerTickEvents.START_SERVER_TICK.register("
    server_end = "        ServerTickEvents.END_SERVER_TICK.register("
    after_server_hooks = "\n    }\n\n    private fun onStartWorldTick("
    if minecraft_env.count(server_start) != 1 or minecraft_env.count(server_end) != 1:
        raise RuntimePreparationError(
            "lockstep server hook anchors must occur exactly once"
        )
    start_index = minecraft_env.index(server_start)
    end_index = minecraft_env.index(server_end, start_index)
    start_hook = """        ServerTickEvents.START_SERVER_TICK.register(
            ServerTickEvents.StartTick { _: MinecraftServer ->
                Unit
            },
        )
"""
    minecraft_env = (
        minecraft_env[:start_index]
        + start_hook
        + minecraft_env[end_index:]
    )
    if minecraft_env.count(after_server_hooks) != 1:
        raise RuntimePreparationError(
            "lockstep post-server hook anchor must occur exactly once"
        )
    end_index = minecraft_env.index(server_end)
    after_index = minecraft_env.index(after_server_hooks, end_index)
    end_hook = """        ServerTickEvents.END_SERVER_TICK.register(
            ServerTickEvents.EndTick { server: MinecraftServer ->
                try {
                    val work = lockstepServerWork
                    if (
                        work.kind == ServerWorkKind.ARM ||
                        work.kind == ServerWorkKind.ACTION
                    ) {
                        LockstepTrace.record(
                            event = if (work.kind == ServerWorkKind.ARM) {
                                "server_arm_complete"
                            } else {
                                "server_tick_complete"
                            },
                            generation = work.generation,
                            serverWorldTime = server.overworld.time,
                        )
                    }
                    tickSynchronizer.completeServerTick(
                        work,
                        server.overworld.time,
                    )
                    val nextWork = tickSynchronizer.beginServerTick()
                    when (nextWork.kind) {
                        ServerWorkKind.FREE -> Unit
                        ServerWorkKind.RESET -> server.tickManager.setFrozen(false)
                        ServerWorkKind.ARM -> {
                            server.tickManager.setFrozen(true)
                            server.sendTimeUpdatePackets()
                        }
                        ServerWorkKind.ACTION -> server.tickManager.step(1)
                    }
                    lockstepServerWork = nextWork
                } catch (error: Exception) {
                    tickSynchronizer.terminate()
                    throw error
                }
            },
        )
        net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents.CLIENT_STOPPING.register {
            tickSynchronizer.terminate()
        }
"""
    minecraft_env = (
        minecraft_env[:end_index]
        + end_hook
        + minecraft_env[after_index:]
    )

    action_tail_start = """        try {
            csvLogger.log("Will Read action")
"""
    action_tail_end = "\n    }\n\n    private fun sendSetScreenNull("
    if (
        minecraft_env.count(action_tail_start) != 1
        or minecraft_env.count(action_tail_end) != 1
    ):
        raise RuntimePreparationError(
            "lockstep legacy action hook anchors must occur exactly once"
        )
    action_start_index = minecraft_env.index(action_tail_start)
    action_end_index = minecraft_env.index(action_tail_end, action_start_index)
    minecraft_env = (
        minecraft_env[:action_start_index]
        + "        return\n"
        + minecraft_env[action_end_index:]
    )

    on_start_world = "    private fun onStartWorldTick("
    if minecraft_env.count(on_start_world) != 1:
        raise RuntimePreparationError(
            "lockstep world-start function anchor must occur exactly once"
        )
    function_index = minecraft_env.index(on_start_world)
    lockstep_functions = """    private fun onLockstepStartClientTick(
        client: MinecraftClient,
        messageIO: MessageIO,
    ) {
        LockstepClientSimulationGate.beginCycle(lockstepClientPhase == LockstepClientPhase.INITIALIZING)
        val player = client.player ?: return
        when (lockstepClientPhase) {
            LockstepClientPhase.INITIALIZING -> return
            LockstepClientPhase.SETTLE -> {
                return
            }
            LockstepClientPhase.ACTION -> Unit
        }
        try {
            csvLogger.log("Will read lockstep action")
            val action = messageIO.readAction()
            ioPhase = IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION
            skipSync = false
            for (command in action.commandsList) {
                if (handleCommand(command, client, player)) {
                    if (command.startsWith("fastreset")) {
                        beginLockstepReset(player, client)
                    }
                    return
                }
            }
            if (player.isDead) return
            if (client.currentScreen is DeathScreen) sendSetScreenNull(client)
            if (applyAction(action, player, client)) return
            LockstepClientSimulationGate.admitAction()
            lockstepActionPending = true
        } catch (error: SocketTimeoutException) {
            csvLogger.log("Lockstep action read timeout")
        } catch (error: IOException) {
            tickSynchronizer.terminate()
            error.printStackTrace()
            exitProcess(-1)
        } catch (error: Exception) {
            tickSynchronizer.terminate()
            error.printStackTrace()
            exitProcess(-2)
        }
    }

    private fun onLockstepEndWorldTick(
        messageIO: MessageIO,
        world: ClientWorld,
    ) {
        try {
            onLockstepEndWorldTickUnsafe(messageIO, world)
        } catch (error: Exception) {
            tickSynchronizer.terminate()
            throw error
        }
    }

    private fun onLockstepEndWorldTickUnsafe(
        messageIO: MessageIO,
        world: ClientWorld,
    ) {
        when (lockstepClientPhase) {
            LockstepClientPhase.INITIALIZING -> {
                if (
                    resetPhase != ResetPhase.END_RESET ||
                    (
                        ioPhase !=
                            IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION &&
                            ioPhase != IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION
                    )
                ) return
                val completion = tickSynchronizer.requestArmAndWait()
                lockstepServerWorldTime = completion.serverWorldTime
                lockstepGeneration = 0L
                lockstepSettleTarget = completion.serverWorldTime
                lockstepSettleStartedAtNanos = System.nanoTime()
                lockstepClientPhase = LockstepClientPhase.SETTLE
                return
            }
            LockstepClientPhase.ACTION -> {
                if (!lockstepActionPending) {
                    failLockstep("lockstep action phase ended without an action")
                }
                val completion = tickSynchronizer.submitActionAndWait()
                lockstepActionPending = false
                lockstepGeneration = completion.generation
                lockstepServerWorldTime = completion.serverWorldTime
                lockstepSettleTarget =
                    (lastEmittedClientWorldTime
                        ?: failLockstep("lockstep client baseline is missing")) + 1L
                lockstepSettleStartedAtNanos = System.nanoTime()
                lockstepClientPhase = LockstepClientPhase.SETTLE
                return
            }
            LockstepClientPhase.SETTLE -> Unit
        }

        val settleStartedAtNanos = lockstepSettleStartedAtNanos
            ?: failLockstep("lockstep client settle timer is missing")
        if (
            System.nanoTime() - settleStartedAtNanos > LOCKSTEP_SETTLE_TIMEOUT_NANOS
        ) {
            failLockstep("lockstep client settle timed out")
        }
        val target = lockstepSettleTarget
        if (target != null) {
            if (world.time > target) {
                failLockstep(
                    "lockstep client world time overshot target: ${world.time} > $target",
                )
            }
            if (world.time < target) return
        }
        LockstepTrace.record(
            event = "client_observation",
            generation = lockstepGeneration,
            serverWorldTime = lockstepServerWorldTime,
            clientWorldTime = world.time,
        )
        sendObservation(messageIO, world)
        lastEmittedClientWorldTime = world.time
        lockstepSettleTarget = null
        lockstepSettleStartedAtNanos = null
        lockstepClientPhase = LockstepClientPhase.ACTION
    }

    private fun beginLockstepReset(
        player: ClientPlayerEntity,
        client: MinecraftClient,
    ) {
        applyAction(ActionSpaceMessageV2.getDefaultInstance(), player, client)
        tickSynchronizer.requestReset()
        lockstepClientPhase = LockstepClientPhase.INITIALIZING
        lockstepActionPending = false
        lockstepGeneration = 0L
        lockstepServerWorldTime = null
        lockstepSettleTarget = null
        lockstepSettleStartedAtNanos = null
        lastEmittedClientWorldTime = null
    }

    private fun failLockstep(message: String): Nothing {
        tickSynchronizer.terminate()
        error(message)
    }

"""
    minecraft_env = (
        minecraft_env[:function_index]
        + lockstep_functions
        + minecraft_env[function_index:]
    )
    try:
        minecraft_env_path.write_text(minecraft_env, encoding="utf-8")
    except OSError as error:
        raise RuntimePreparationError(
            f"cannot write lockstep hook target: {error}"
        ) from error
    return ("install lockstep overlay", "install generation-aware server hooks")


def _replace_exact_once(
    value: str,
    old: str,
    new: str,
    *,
    description: str,
) -> str:
    if value.count(old) != 1:
        raise RuntimePreparationError(
            f"structured runtime {description} anchor must occur exactly once"
        )
    return value.replace(old, new, 1)


def _patch_structured_action_boundary(minecraft_env: str) -> str:
    """Read before tickEntities consumes player input; keep reset initialization in world tick."""
    start = '        try {\n            csvLogger.log("Will Read action")\n'
    end = '\n    }\n\n    private fun sendSetScreenNull('
    if minecraft_env.count(start) != 1 or minecraft_env.count(end) != 1:
        raise RuntimePreparationError("structured client action boundary anchors changed")
    start_index, end_index = minecraft_env.index(start), minecraft_env.index(end)
    if end_index <= start_index:
        raise RuntimePreparationError("structured client action boundary ordering changed")
    action_tail = minecraft_env[start_index:end_index]
    replacement = """        return
    }

    private fun onMc2pStartClientTick(client: MinecraftClient, messageIO: MessageIO) {
        val player = client.player ?: return
        if (resetPhase != ResetPhase.END_RESET) return
        if (ioPhase != IOPhase.SENT_OBSERVATION_SHOULD_READ_ACTION &&
            ioPhase != IOPhase.GOT_INITIAL_ENVIRONMENT_SENT_OBSERVATION_SKIP_SEND_OBSERVATION) return
""" + action_tail
    minecraft_env = minecraft_env[:start_index] + replacement + minecraft_env[end_index:]
    return _replace_exact_once(
        minecraft_env,
        '                csvLogger.profileEndPrint("Minecraft_env/onInitialize/ClientTick")\n',
        '                onMc2pStartClientTick(client, messageIO)\n'
        '                csvLogger.profileEndPrint("Minecraft_env/onInitialize/ClientTick")\n',
        description="client-start action dispatch",
    )


def _patch_structured_runtime(sandbox_root: Path, clock_mode: CraftGroundClockModeV0) -> tuple[str, ...]:
    overlays = {
        **BEHAVIOR_OVERLAYS,
        CLIENT_SAMPLE_CLOCK_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientSampleClock.java",
        CLIENT_ENTITY_NAME_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientEntityName.java",
        CLIENT_ENTITY_INDEX_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientEntityIndex.java",
        CLIENT_DAMAGE_EVENT_BUFFER_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientDamageEventBuffer.java",
        CLIENT_CROSSHAIR_ACCESS_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientCrosshairAccess.java",
        CLIENT_BLOCK_OBSERVATION_V3_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientBlockObservationV3.java",
        CLIENT_BLOCK_PARITY_DIAGNOSTICS_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientBlockParityDiagnostics.java",
        CLIENT_OBSERVATION_REQUEST_V3_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientObservationRequestV3.java",
        STRUCTURED_DIAGNOSTICS_RELATIVE_PATH: (
            STRUCTURED_OVERLAY_ROOT / "StructuredObservationDiagnostics.kt"
        ),
        RENDER_MIXIN_RELATIVE_PATH: STRUCTURED_OVERLAY_ROOT / "RenderMixin.java",
        GAME_RENDERER_MIXIN_RELATIVE_PATH: (
            STRUCTURED_OVERLAY_ROOT / "GameRendererMixin.java"
        ),
        WINDOW_OFFSCREEN_MIXIN_RELATIVE_PATH: (
            STRUCTURED_OVERLAY_ROOT / "WindowOffScreenMixin.java"
        ),
        CLIENT_OBSERVATION_COLLECTOR_RELATIVE_PATH: (
            OBSERVATION_OVERLAY_ROOT / "ClientObservationCollector.java"
        ),
        CLIENT_OBSERVATION_JSON_RELATIVE_PATH: (
            OBSERVATION_OVERLAY_ROOT / "ClientObservationJson.java"
        ),
        CLIENT_GUI_SESSION_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientGuiSession.java",
        CLIENT_GUI_PROPERTIES_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientGuiProperties.java",
        CLIENT_GUI_PROPERTY_ACCESS_RELATIVE_PATH: OBSERVATION_OVERLAY_ROOT / "ClientGuiPropertyAccess.java",
        CLIENT_OBSERVATION_BRIDGE_RELATIVE_PATH: (
            STRUCTURED_OVERLAY_ROOT / "ClientObservationCraftGroundBridge.java"
        ),
    }
    for relative, source in overlays.items():
        if not source.is_file():
            raise RuntimePreparationError(f"structured overlay is missing: {source}")
        destination = sandbox_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    minecraft_env_path = sandbox_root / Path(MINECRAFT_ENV_RELATIVE_PATH)
    framebuffer_path = sandbox_root / Path(FRAMEBUFFER_CAPTURER_RELATIVE_PATH)
    initializer_path = sandbox_root / Path(
        "src/main/java/com/kyhsgeekcode/minecraftenv/EnvironmentInitializer.kt"
    )
    mixin_path = sandbox_root / Path(MIXIN_RELATIVE_PATH)
    build_gradle_path = sandbox_root / "build.gradle"
    try:
        minecraft_env = minecraft_env_path.read_text(encoding="utf-8")
        framebuffer = framebuffer_path.read_text(encoding="utf-8")
        initializer = initializer_path.read_text(encoding="utf-8")
        mixin_config = json.loads(mixin_path.read_text(encoding="utf-8"))
        build_gradle = build_gradle_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimePreparationError(
            f"cannot read structured patch target: {error}"
        ) from error

    clients = mixin_config.get("client")
    if not isinstance(clients, list) or "GameRendererMixin" in clients:
        raise RuntimePreparationError("structured mixin client list is invalid")
    clients.append("GameRendererMixin")
    clients.extend(("BehaviorKeyboardMixin", "BehaviorMouseMixin", "HandledScreenRenderMixin"))
    clients.extend(("BehaviorPlayerMixin", "BehaviorInputMixin"))
    clients.append("ScreenHandlerPropertiesMixin")
    clients.extend(("ClientClockTickMixin", "ClientClockWorldMixin", "ClientClockPacketMixin"))

    build_gradle = _replace_exact_once(
        build_gradle,
        "    cmakeArgs += 'src/main/cpp'\n",
        """    def localGlmSource = System.getenv('CRAFTGROUND_GLM_SOURCE')
    if (localGlmSource) {
        def localGlmDirectory = file(localGlmSource)
        if (!localGlmDirectory.isDirectory()) {
            throw new GradleException('CRAFTGROUND_GLM_SOURCE is not a directory')
        }
        cmakeArgs += "-DFETCHCONTENT_SOURCE_DIR_GLM=${localGlmDirectory.absolutePath}"
    }
    cmakeArgs += 'src/main/cpp'
""",
        description="local GLM source override",
    )

    glew_block = """        if (FramebufferCapturer.checkGLEW()) {
            printWithTime("GLEW initialized")
        } else {
            printWithTime("GLEW not initialized")
            throw RuntimeException("GLEW not initialized")
        }
"""
    guarded_glew_block = """        val structuredOnly = StructuredObservationDiagnostics.STRUCTURED_ONLY
        if (!structuredOnly && !FramebufferCapturer.checkGLEW()) {
            printWithTime("GLEW not initialized")
            throw RuntimeException("GLEW not initialized")
        }
"""
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "import com.google.protobuf.ByteString\n",
        """import com.google.protobuf.ByteString
import com.mc2p.observation.ClientObservationCollector
import com.mc2p.diagnostics.ClientTimeDiagnostics
""",
        description="V2 collector import",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "    private var skipSync = false\n",
        """    private var skipSync = false
    private var mc2pObservationGeneration = -1L
""",
        description="V2 generation field",
    )
    minecraft_env = _patch_structured_action_boundary(minecraft_env)
    send_header = "    private fun sendObservation("
    if minecraft_env.count(send_header) != 1:
        raise RuntimePreparationError("structured reset observation hook must occur exactly once")
    send_start = minecraft_env.index(send_header)
    send_body = minecraft_env.index(") {", send_start) + len(") {")
    minecraft_env = (minecraft_env[:send_body]
        + "\n        if (resetPhase != ResetPhase.END_RESET) return"
        + minecraft_env[send_body:])
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "            val action = messageIO.readAction()\n",
        """            val action = messageIO.readAction()
            ClientBehaviorCraftGroundBridge.validateBeforeDispatch(client, action)
            if (action.commandsList.any { it == "fastreset" || it == "fastreset " }) {
                mc2pObservationGeneration = -1L
            }
""",
        description="reject mixed formal envelopes before any command dispatch",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        """    private fun applyAction(
        actionDict: ActionSpaceMessageV2,
        player: ClientPlayerEntity,
        client: MinecraftClient,
    ): Boolean {
""",
        """    private fun applyAction(
        actionDict: ActionSpaceMessageV2,
        player: ClientPlayerEntity,
        client: MinecraftClient,
    ): Boolean {
        if (ClientBehaviorCraftGroundBridge.apply(client, actionDict)) return false
""",
        description="formal client behavior dispatch before legacy keyboard conversion",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "            if (ioPhase == IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION) {\n",
        """            val mc2pGeneration =
                if (ioPhase == IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION) 0L
                else mc2pObservationGeneration + 1L
            mc2pObservationGeneration = mc2pGeneration
            val mc2pPayload =
                ClientObservationCraftGroundBridge.collect(
                    client,
                    mc2pGeneration,
                )
            val mc2pObservationWithPayload =
                ClientObservationCraftGroundBridge.attach(
                    observationSpaceMessage,
                    mc2pPayload,
                )
            val mc2pObservationSpaceMessage = ClientBehaviorCraftGroundBridge.attach(
                client, mc2pObservationWithPayload, mc2pGeneration,
            )
            ClientTimeDiagnostics.observation(client, mc2pGeneration)
            if (ioPhase == IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION) {
""",
        description="V2 payload attachment",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        glew_block,
        guarded_glew_block,
        description="GLEW guard",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        """        if (initialEnvironment.screenEncodingMode == FramebufferCapturer.ZEROCOPY) {
            FramebufferCapturer.initializeZeroCopy(
""",
        """        if (!structuredOnly && initialEnvironment.screenEncodingMode == FramebufferCapturer.ZEROCOPY) {
            FramebufferCapturer.initializeZeroCopy(
""",
        description="zero-copy guard",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "            if (initialEnvironment.eyeDistance > 0) {\n",
        """            if (structuredOnly) {
                imageByteString1 = ByteString.EMPTY
                imageByteString2 = ByteString.EMPTY
            } else if (initialEnvironment.eyeDistance > 0) {
""",
        description="capture branch",
    )
    minecraft_env = _replace_exact_once(
        minecraft_env,
        "            messageIO.writeObservation(observationSpaceMessage)\n",
        """            StructuredObservationDiagnostics.recordObservation(
                client.window.handle,
                observationSpaceMessage.image.size(),
                observationSpaceMessage.image2.size(),
                FramebufferCapturer.framebufferCaptureCalls,
                FramebufferCapturer.imageEncodeCalls,
            )
            messageIO.writeObservation(mc2pObservationSpaceMessage)
""",
        description="diagnostics hook",
    )

    framebuffer = _replace_exact_once(
        framebuffer,
        "object FramebufferCapturer {\n",
        """object FramebufferCapturer {
    var framebufferCaptureCalls: Long = 0L
        private set
    var imageEncodeCalls: Long = 0L
        private set

""",
        description="framebuffer counters",
    )
    framebuffer = _replace_exact_once(
        framebuffer,
        "    ): ByteString {\n        if (encodingMode == ZEROCOPY) {\n",
        """    ): ByteString {
        framebufferCaptureCalls += 1L
        if (encodingMode == ZEROCOPY) {
""",
        description="capture counter",
    )
    framebuffer = _replace_exact_once(
        framebuffer,
        "            return captureFramebufferZerocopyImpl(\n",
        """            imageEncodeCalls += 1L
            return captureFramebufferZerocopyImpl(
""",
        description="zero-copy encode counter",
    )
    framebuffer = _replace_exact_once(
        framebuffer,
        "            return captureFramebufferImpl(\n",
        """            imageEncodeCalls += 1L
            return captureFramebufferImpl(
""",
        description="readback encode counter",
    )
    initializer = _replace_exact_once(
        initializer,
        "            GLFW.glfwIconifyWindow(window.handle)\n",
        "            GLFW.glfwHideWindow(window.handle)\n",
        description="persistent hidden window",
    )
    initializer = _replace_exact_once(
        initializer,
        "                createButton?.onPress()\n                finishedEnteringWorld = true\n",
        """                // A declared vanilla world-generation option, never an actor operation.
                screen.worldCreator.setBonusChestEnabled(initialEnvironment.bonusChest)
                if (initialEnvironment.worldTypeArgs.isNotBlank()) {
                    // Same preset parser and generator modifier used by the vanilla creation GUI.
                    // This is fixture initialization, never the shared actor execution path.
                    require(initialEnvironment.worldTypeArgs == "minecraft:bedrock,2*minecraft:dirt,minecraft:furnace;minecraft:plains")
                    val creator = screen.worldCreator
                    val holder = creator.generatorOptionsHolder
                    val registries = holder.combinedRegistryManager
                    val previous = holder.selectedDimensions().chunkGenerator as net.minecraft.world.gen.chunk.FlatChunkGenerator
                    val config = net.minecraft.client.gui.screen.world.PresetsScreen.parsePresetString(
                        net.minecraft.registry.Registries.BLOCK.readOnlyWrapper,
                        registries.getWrapperOrThrow(net.minecraft.registry.RegistryKeys.BIOME),
                        registries.getWrapperOrThrow(net.minecraft.registry.RegistryKeys.STRUCTURE_SET),
                        registries.getWrapperOrThrow(net.minecraft.registry.RegistryKeys.PLACED_FEATURE),
                        initialEnvironment.worldTypeArgs, previous.config)
                    creator.applyModifier { registryManager, dimensions ->
                        dimensions.with(registryManager, net.minecraft.world.gen.chunk.FlatChunkGenerator(config))
                    }
                }
                createButton?.onPress()
                finishedEnteringWorld = true
""",
        description="normal bonus-chest world creation option",
    )
    initializer = _replace_exact_once(
        initializer,
        "        minecraftServer?.getSavePath(WorldSavePath.RESOURCES_ZIP)?.let { targetZipPath ->\n",
        """        minecraftServer?.getSavePath(WorldSavePath.RESOURCES_ZIP)?.let { targetZipPath ->
            if (initialEnvironment.resourceZipPath.isBlank()) return@let
""",
        description="empty resource-pack path guard",
    )
    initializer = _replace_exact_once(
        initializer,
        "        minecraftServer?.getSavePath(WorldSavePath.ROOT)?.let { rootPath ->\n",
        """        minecraftServer?.getSavePath(WorldSavePath.ROOT)?.let { rootPath ->
            if (initialEnvironment.mapDirPath.isBlank()) return@let
""",
        description="empty map-data path guard",
    )
    try:
        minecraft_env_path.write_text(minecraft_env, encoding="utf-8")
        framebuffer_path.write_text(framebuffer, encoding="utf-8")
        initializer_path.write_text(initializer, encoding="utf-8")
        mixin_path.write_text(
            json.dumps(mixin_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        build_gradle_path.write_text(build_gradle, encoding="utf-8")
    except OSError as error:
        raise RuntimePreparationError(
            f"cannot write structured patch target: {error}"
        ) from error
    return (
        "install structured observation overlay",
        "guard framebuffer capture and image encoding",
        "install invisible window and renderWorld-cancelling mixins",
        "ignore empty resource-pack and map-data paths",
        "allow verified local GLM source for offline native builds",
    )


def _current_patch_recipe_fingerprint(
    clock_mode: CraftGroundClockModeV0,
    observation_mode: CraftGroundObservationModeV0 = (
        CraftGroundObservationModeV0.STRUCTURED_ONLY
    ),
    observation_schema_version: str = OBSERVATION_V2,
) -> str:
    """Bind a sandbox to the repository code and overlays that produced it."""

    if not isinstance(clock_mode, CraftGroundClockModeV0):
        raise RuntimePreparationError("invalid CraftGround clock mode")
    if not isinstance(observation_mode, CraftGroundObservationModeV0):
        raise RuntimePreparationError("invalid CraftGround observation mode")
    digest = hashlib.sha256()
    digest.update(SANDBOX_SCHEMA_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(clock_mode.value.encode("utf-8"))
    digest.update(b"\0")
    digest.update(observation_mode.value.encode("utf-8"))
    if observation_schema_version not in (OBSERVATION_V2, OBSERVATION_V3):
        raise RuntimePreparationError("invalid observation schema")
    digest.update(observation_schema_version.encode("utf-8"))
    try:
        digest.update(Path(__file__).resolve().read_bytes())
        if clock_mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
            for relative in (
                LOCKSTEP_CONTROLLER_RELATIVE_PATH,
                LOCKSTEP_TRACE_RELATIVE_PATH,
                LOCKSTEP_PLAYER_GATE_RELATIVE_PATH,
                LOCKSTEP_PLAYER_MIXIN_RELATIVE_PATH,
            ):
                digest.update(b"\0")
                digest.update(relative.encode("utf-8"))
                name = Path(relative).name
                if relative == LOCKSTEP_CONTROLLER_RELATIVE_PATH and observation_mode is CraftGroundObservationModeV0.POV_DEBUG:
                    name = "LegacyTickSynchronizer.kt"
                digest.update((LOCKSTEP_OVERLAY_ROOT / name).read_bytes())
        if observation_mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
            if clock_mode in (CraftGroundClockModeV0.REFERENCE_20_TPS, CraftGroundClockModeV0.LOCKSTEP_ACCELERATED):
                for source in recipe_sources():
                    digest.update(b"\0")
                    digest.update(source.name.encode("utf-8"))
                    digest.update(source.read_bytes())
                if clock_mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
                    digest.update(Path(__file__).with_name("craftground_nonblocking_lockstep.py").read_bytes())
            for relative, source in sorted(BEHAVIOR_OVERLAYS.items()):
                digest.update(b"\0")
                digest.update(relative.encode("utf-8"))
                digest.update(source.read_bytes())
            for name in (
                "StructuredObservationDiagnostics.kt",
                "RenderMixin.java",
                "GameRendererMixin.java",
                "WindowOffScreenMixin.java",
                "ClientObservationCraftGroundBridge.java",
            ):
                digest.update(b"\0")
                digest.update(name.encode("utf-8"))
                digest.update((STRUCTURED_OVERLAY_ROOT / name).read_bytes())
            for name in (
                "ClientObservationCollector.java",
                "ClientBlockObservationV3.java", "ClientBlockParityDiagnostics.java", "ClientObservationRequestV3.java",
                "ClientSampleClock.java",
                "ClientEntityName.java", "ClientCrosshairAccess.java",
                "ClientEntityIndex.java",
                "ClientDamageEventBuffer.java",
                "ClientObservationJson.java",
                "ClientGuiSession.java",
                "ClientGuiProperties.java", "ClientGuiPropertyAccess.java",
            ):
                digest.update(b"\0")
                digest.update(name.encode("utf-8"))
                digest.update((OBSERVATION_OVERLAY_ROOT / name).read_bytes())
    except OSError as error:
        raise RuntimePreparationError(
            f"cannot fingerprint repository patch recipe: {error}"
        ) from error
    return digest.hexdigest()


def _fingerprint_pairs(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value).casefold()) for key, value in values.items()))


def _manifest_value(manifest: CraftGroundSandboxManifestV0) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "craftground_version": manifest.craftground_version,
        "runtime_version": manifest.runtime_version,
        "clock_mode": manifest.clock_mode.value,
        "observation_mode": manifest.observation_mode.value,
        "observation_schema_version": manifest.observation_schema_version,
        "source_root": manifest.source_root,
        "sandbox_root": manifest.sandbox_root,
        "source_fingerprints": [list(item) for item in manifest.source_fingerprints],
        "output_fingerprints": [list(item) for item in manifest.output_fingerprints],
        "patch_recipe_fingerprint": manifest.patch_recipe_fingerprint,
        "patch_operations": list(manifest.patch_operations),
        "created_at_utc": manifest.created_at_utc,
    }


def _write_manifest(path: Path, manifest: CraftGroundSandboxManifestV0) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(_manifest_value(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_fingerprint_pairs(name: str, value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise RuntimePreparationError(f"manifest {name} must be a list")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise RuntimePreparationError(f"manifest {name} entry is invalid")
        relative = _normal_relative_path(str(item[0]))
        sha256 = str(item[1]).casefold()
        if not _SHA256.fullmatch(sha256):
            raise RuntimePreparationError(f"manifest {name} hash is invalid")
        result.append((relative, sha256))
    if len({relative for relative, _ in result}) != len(result):
        raise RuntimePreparationError(f"manifest {name} contains duplicate paths")
    return tuple(sorted(result))


def load_sandbox_manifest(sandbox_root: Path) -> CraftGroundSandboxManifestV0:
    root = Path(sandbox_root).resolve()
    manifest_path = root / SANDBOX_MANIFEST_NAME
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimePreparationError(f"cannot read sandbox manifest: {error}") from error
    expected_keys = {
        "schema_version",
        "craftground_version",
        "runtime_version",
        "clock_mode",
        "observation_mode",
        "source_root",
        "sandbox_root",
        "source_fingerprints",
        "output_fingerprints",
        "patch_recipe_fingerprint",
        "patch_operations",
        "created_at_utc",
    }
    if not isinstance(value, dict):
        raise RuntimePreparationError("sandbox manifest keys are invalid")
    legacy = value.get("schema_version") == "mc2p.craftground-sandbox.v2"
    if not legacy:
        expected_keys.add("observation_schema_version")
    if set(value) != expected_keys:
        raise RuntimePreparationError("sandbox manifest keys are invalid")
    if value["schema_version"] not in (SANDBOX_SCHEMA_VERSION, "mc2p.craftground-sandbox.v2"):
        raise RuntimePreparationError("sandbox manifest schema version is invalid")
    observation_schema = OBSERVATION_V2 if legacy else value["observation_schema_version"]
    if type(observation_schema) is not str or observation_schema not in (OBSERVATION_V2, OBSERVATION_V3):
        raise RuntimePreparationError("sandbox observation schema is invalid")
    try:
        mode = CraftGroundClockModeV0(str(value["clock_mode"]))
    except ValueError as error:
        raise RuntimePreparationError("sandbox manifest clock mode is invalid") from error
    try:
        observation_mode = CraftGroundObservationModeV0(
            str(value["observation_mode"])
        )
    except ValueError as error:
        raise RuntimePreparationError(
            "sandbox manifest observation mode is invalid"
        ) from error
    source_root = Path(str(value["source_root"]))
    recorded_sandbox = Path(str(value["sandbox_root"]))
    if not source_root.is_absolute() or not recorded_sandbox.is_absolute():
        raise RuntimePreparationError("sandbox manifest paths must be absolute")
    patches = value["patch_operations"]
    if not isinstance(patches, list) or not all(
        isinstance(item, str) and item for item in patches
    ):
        raise RuntimePreparationError("sandbox manifest patch operations are invalid")
    patch_recipe_fingerprint = str(value["patch_recipe_fingerprint"]).casefold()
    if not _SHA256.fullmatch(patch_recipe_fingerprint):
        raise RuntimePreparationError(
            "sandbox manifest patch recipe fingerprint is invalid"
        )
    return CraftGroundSandboxManifestV0(
        schema_version=value["schema_version"],
        craftground_version=str(value["craftground_version"]),
        runtime_version=str(value["runtime_version"]),
        clock_mode=mode,
        observation_mode=observation_mode,
        source_root=str(source_root),
        sandbox_root=str(recorded_sandbox),
        source_fingerprints=_parse_fingerprint_pairs(
            "source_fingerprints", value["source_fingerprints"]
        ),
        output_fingerprints=_parse_fingerprint_pairs(
            "output_fingerprints", value["output_fingerprints"]
        ),
        patch_recipe_fingerprint=patch_recipe_fingerprint,
        patch_operations=tuple(patches),
        created_at_utc=str(value["created_at_utc"]),
        observation_schema_version=observation_schema,
    )


def validate_sandbox_for_mode(
    sandbox_root: Path,
    clock_mode: CraftGroundClockModeV0,
    observation_mode: CraftGroundObservationModeV0 = (
        CraftGroundObservationModeV0.STRUCTURED_ONLY
    ),
    *, observation_schema_version: str | None = None,
) -> CraftGroundSandboxManifestV0:
    if not isinstance(clock_mode, CraftGroundClockModeV0):
        raise RuntimePreparationError("invalid CraftGround clock mode")
    if not isinstance(observation_mode, CraftGroundObservationModeV0):
        raise RuntimePreparationError("invalid CraftGround observation mode")
    root = Path(sandbox_root).resolve()
    if not root.is_dir():
        raise RuntimePreparationError(f"sandbox is not a directory: {root}")
    manifest = load_sandbox_manifest(root)
    if manifest.schema_version != SANDBOX_SCHEMA_VERSION:
        raise RuntimePreparationError("legacy sandbox manifest is read-only; prepare a fresh sandbox")
    if Path(manifest.sandbox_root) != root:
        raise RuntimePreparationError("sandbox manifest root does not match its location")
    if manifest.clock_mode is not clock_mode:
        raise RuntimePreparationError(
            f"sandbox clock mode is {manifest.clock_mode.value}, not {clock_mode.value}"
        )
    if manifest.observation_mode is not observation_mode:
        raise RuntimePreparationError(
            "sandbox observation mode is "
            f"{manifest.observation_mode.value}, not {observation_mode.value}"
        )
    if observation_schema_version is not None and manifest.observation_schema_version != observation_schema_version:
        raise RuntimePreparationError("sandbox observation schema mismatch")
    current_recipe = _current_patch_recipe_fingerprint(clock_mode, observation_mode, manifest.observation_schema_version)
    if manifest.patch_recipe_fingerprint != current_recipe:
        raise RuntimePreparationError("sandbox patch recipe fingerprint mismatch")
    recorded_outputs = dict(manifest.output_fingerprints)
    actual_outputs = capture_runtime_source_fingerprints(root, recorded_outputs)
    mismatched = [
        relative
        for relative, expected in recorded_outputs.items()
        if actual_outputs.get(relative) != expected
    ]
    if mismatched:
        raise RuntimePreparationError(
            f"sandbox output fingerprint mismatch: {', '.join(sorted(mismatched))}"
        )
    return manifest


def _matching_reusable_sandbox(
    candidate: Path,
    *,
    source_fingerprints: Mapping[str, str],
    clock_mode: CraftGroundClockModeV0,
    observation_mode: CraftGroundObservationModeV0,
    craftground_version: str,
    runtime_version: str,
    observation_schema_version: str,
) -> CraftGroundSandboxManifestV0 | None:
    try:
        manifest = validate_sandbox_for_mode(
            candidate,
            clock_mode,
            observation_mode,
            observation_schema_version=observation_schema_version,
        )
    except RuntimePreparationError:
        return None
    if dict(manifest.source_fingerprints) != dict(source_fingerprints):
        return None
    if (
        manifest.craftground_version != craftground_version
        or manifest.runtime_version != runtime_version
    ):
        return None
    return manifest


def prepare_runtime_sandbox(
    *,
    source_root: Path,
    sandbox_parent: Path,
    sandbox_id: str,
    clock_mode: CraftGroundClockModeV0,
    observation_mode: CraftGroundObservationModeV0 = (
        CraftGroundObservationModeV0.STRUCTURED_ONLY
    ),
    observation_schema_version: str = OBSERVATION_V2,
    expected_fingerprints: Mapping[
        str, str
    ] = MC121_RUNTIME_0_1_0_FINGERPRINTS,
) -> PreparedCraftGroundRuntimeV0:
    if (
        not isinstance(sandbox_id, str)
        or not _SAFE_COMPONENT.fullmatch(sandbox_id)
        or sandbox_id in {".", ".."}
    ):
        raise RuntimePreparationError("sandbox_id must be one safe path component")
    if not isinstance(clock_mode, CraftGroundClockModeV0):
        raise RuntimePreparationError("invalid CraftGround clock mode")
    if not isinstance(observation_mode, CraftGroundObservationModeV0):
        raise RuntimePreparationError("invalid CraftGround observation mode")
    if type(observation_schema_version) is not str or observation_schema_version not in (OBSERVATION_V2, OBSERVATION_V3):
        raise RuntimePreparationError("invalid observation schema")
    if observation_schema_version == OBSERVATION_V3 and observation_mode is not CraftGroundObservationModeV0.STRUCTURED_ONLY:
        raise RuntimePreparationError("V3 requires structured_only runtime")
    source = Path(source_root).resolve()
    expected = {
        _normal_relative_path(str(relative)): str(sha256).casefold()
        for relative, sha256 in expected_fingerprints.items()
    }
    if not expected or any(not _SHA256.fullmatch(value) for value in expected.values()):
        raise RuntimePreparationError("expected source fingerprint map is invalid")
    source_fingerprints = capture_runtime_source_fingerprints(source, expected)
    mismatched = [
        relative
        for relative, sha256 in expected.items()
        if source_fingerprints.get(relative) != sha256
    ]
    if mismatched:
        raise RuntimePreparationError(
            f"runtime source fingerprint mismatch: {', '.join(sorted(mismatched))}"
        )
    craftground_version = _version("craftground")
    runtime_version = _version("craftground-runtime-mc121")
    patch_recipe_fingerprint = _current_patch_recipe_fingerprint(
        clock_mode,
        observation_mode,
        observation_schema_version,
    )

    parent = Path(sandbox_parent).resolve()
    base = parent / sandbox_id
    suffix = 1
    while True:
        candidate = base if suffix == 1 else parent / f"{sandbox_id}-{suffix}"
        if not candidate.exists():
            break
        reusable = _matching_reusable_sandbox(
            candidate,
            source_fingerprints=source_fingerprints,
            clock_mode=clock_mode,
            observation_mode=observation_mode,
            craftground_version=craftground_version,
            runtime_version=runtime_version,
            observation_schema_version=observation_schema_version,
        )
        if reusable is not None:
            return PreparedCraftGroundRuntimeV0(candidate, reusable, True)
        suffix += 1

    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{candidate.name}.building-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging, ignore=_ignore_generated)
        patches: tuple[str, ...] = ()
        output_paths = set(expected)
        if clock_mode is CraftGroundClockModeV0.REFERENCE_20_TPS:
            patches = _patch_reference_mixin(staging)
        elif clock_mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
            if observation_mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
                _install_lockstep_overlays(staging, formal=True)
                patches = ("install lockstep overlay", "install generation-aware server hooks")
            else:
                patches = _patch_lockstep_runtime(staging)
            output_paths.update(
                (LOCKSTEP_CONTROLLER_RELATIVE_PATH, LOCKSTEP_TRACE_RELATIVE_PATH,
                 LOCKSTEP_PLAYER_GATE_RELATIVE_PATH, LOCKSTEP_PLAYER_MIXIN_RELATIVE_PATH)
            )
        if observation_mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
            patches += _patch_structured_runtime(staging, clock_mode)
            build = staging / "build.gradle"
            build.write_text(build.read_text(encoding="utf-8") +
                "\n// Frozen observation schema for this fingerprinted sandbox.\n" +
                "tasks.withType(JavaExec).configureEach {\n" +
                f"    systemProperty 'mc2p.observationSchema', '{observation_schema_version}'\n" +
                "}\n", encoding="utf-8")
            if clock_mode is CraftGroundClockModeV0.REFERENCE_20_TPS:
                patches += install_nonblocking_reference(staging)
                output_paths.update(NONBLOCKING_OUTPUTS)
            elif clock_mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
                patches += install_nonblocking_lockstep(staging)
                output_paths.update(NONBLOCKING_OUTPUTS)
            output_paths.update(BEHAVIOR_OVERLAYS)
            output_paths.update(
                (
                    STRUCTURED_DIAGNOSTICS_RELATIVE_PATH,
                    GAME_RENDERER_MIXIN_RELATIVE_PATH,
                    CLIENT_OBSERVATION_COLLECTOR_RELATIVE_PATH,
                    CLIENT_BLOCK_OBSERVATION_V3_RELATIVE_PATH, CLIENT_BLOCK_PARITY_DIAGNOSTICS_RELATIVE_PATH,
                    CLIENT_OBSERVATION_REQUEST_V3_RELATIVE_PATH,
                    CLIENT_OBSERVATION_JSON_RELATIVE_PATH,
                    CLIENT_GUI_SESSION_RELATIVE_PATH,
                    CLIENT_SAMPLE_CLOCK_RELATIVE_PATH,
                    CLIENT_ENTITY_NAME_RELATIVE_PATH, CLIENT_CROSSHAIR_ACCESS_RELATIVE_PATH,
                    CLIENT_ENTITY_INDEX_RELATIVE_PATH,
                    CLIENT_GUI_PROPERTIES_RELATIVE_PATH, CLIENT_GUI_PROPERTY_ACCESS_RELATIVE_PATH,
                    CLIENT_OBSERVATION_BRIDGE_RELATIVE_PATH,
                )
            )
        output_fingerprints = capture_runtime_source_fingerprints(
            staging,
            output_paths,
        )
        manifest = CraftGroundSandboxManifestV0(
            schema_version=SANDBOX_SCHEMA_VERSION,
            craftground_version=craftground_version,
            runtime_version=runtime_version,
            clock_mode=clock_mode,
            observation_mode=observation_mode,
            observation_schema_version=observation_schema_version,
            source_root=str(source),
            sandbox_root=str(candidate),
            source_fingerprints=_fingerprint_pairs(source_fingerprints),
            output_fingerprints=_fingerprint_pairs(output_fingerprints),
            patch_recipe_fingerprint=patch_recipe_fingerprint,
            patch_operations=patches,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        _write_manifest(staging / SANDBOX_MANIFEST_NAME, manifest)
        _rename_directory_with_retry(staging, candidate)
    except Exception as error:
        if isinstance(error, RuntimePreparationError):
            raise
        raise RuntimePreparationError(f"sandbox preparation failed: {error}") from error
    validate_sandbox_for_mode(candidate, clock_mode, observation_mode, observation_schema_version=observation_schema_version)
    return PreparedCraftGroundRuntimeV0(candidate, manifest, False)


def _rename_directory_with_retry(
    source: Path,
    destination: Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Publish a copied sandbox despite short-lived Windows scanner handles."""

    if type(attempts) is not int or attempts <= 0:
        raise ValueError("rename attempts must be a positive integer")
    if delay_seconds < 0:
        raise ValueError("rename delay must be non-negative")
    for attempt in range(1, attempts + 1):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            sleep(delay_seconds)
