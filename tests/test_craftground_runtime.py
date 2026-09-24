from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    RuntimePreparationError,
    capture_runtime_source_fingerprints,
    load_sandbox_manifest,
    prepare_runtime_sandbox,
    validate_sandbox_for_mode,
    _rename_directory_with_retry,
    CLIENT_OBSERVATION_COLLECTOR_RELATIVE_PATH,
    CLIENT_OBSERVATION_JSON_RELATIVE_PATH,
    CLIENT_OBSERVATION_BRIDGE_RELATIVE_PATH,
    OBSERVATION_OVERLAY_ROOT,
)


MIXIN_PATH = Path(
    "src/main/resources/com.kyhsgeekcode.minecraftenv.mixin.json"
)
FIXTURE_FILES = {
    "src/main/java/com/kyhsgeekcode/minecraftenv/EnvironmentInitializer.kt": (
        b"setUnlimitedTPS(); initialExtraCommandsList.forEach {}\n"
        b"            GLFW.glfwIconifyWindow(window.handle)\n"
        b"        minecraftServer?.getSavePath(WorldSavePath.RESOURCES_ZIP)?.let { targetZipPath ->\n"
        b"        minecraftServer?.getSavePath(WorldSavePath.ROOT)?.let { rootPath ->\n"
        b"                createButton?.onPress()\n                finishedEnteringWorld = true\n"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/TickSpeed.kt": b"val mspt = 1L\n",
    "src/main/java/com/kyhsgeekcode/minecraftenv/TickSynchronizer.kt": (
        b"internal class TickSynchronizer { var legacy = true }\n"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt": (
        b"""package com.kyhsgeekcode.minecraftenv

import com.google.protobuf.ByteString

class MinecraftEnv {
    private val tickSynchronizer = TickSynchronizer()
    private var skipSync = false

    fun registerHooks() {
        ClientTickEvents.START_CLIENT_TICK.register(
            ClientTickEvents.StartTick { client: MinecraftClient ->
                initializer.onClientTick(client)
                csvLogger.profileEndPrint("Minecraft_env/onInitialize/ClientTick")
            },
        )
        ClientTickEvents.START_WORLD_TICK.register(
            ClientTickEvents.StartWorldTick { world: ClientWorld ->
                onStartWorldTick(initializer, world, messageIO)
            },
        )
        ClientTickEvents.END_WORLD_TICK.register(
            ClientTickEvents.EndWorldTick { world: ClientWorld ->
                legacyClientEnd(world)
            },
        )
        ServerTickEvents.START_SERVER_TICK.register(
            ServerTickEvents.StartTick { server: MinecraftServer ->
                legacyStart(server)
            },
        )
        ServerTickEvents.END_SERVER_TICK.register(
            ServerTickEvents.EndTick { server: MinecraftServer ->
                legacyEnd(server)
            },
        )
    }

    private fun onStartWorldTick(
        initializer: EnvironmentInitializer,
        world: ClientWorld,
        messageIO: MessageIO,
    ) {
        val client = MinecraftClient.getInstance()
        soundListener!!.onTick()
        try {
            csvLogger.log("Will Read action")
            val action = messageIO.readAction()
            if (applyAction(action, player, client)) return
        } catch (error: Exception) {
            tickSynchronizer.terminate()
        }
    }

    private fun sendSetScreenNull() = Unit

    private fun applyAction(
        actionDict: ActionSpaceMessageV2,
        player: ClientPlayerEntity,
        client: MinecraftClient,
    ): Boolean {
        return false
    }

    private fun sendObservation(messageIO: MessageIO, world: ClientWorld) {
        val client = MinecraftClient.getInstance()
        val player = client.player ?: return
        if (FramebufferCapturer.checkGLEW()) {
            printWithTime("GLEW initialized")
        } else {
            printWithTime("GLEW not initialized")
            throw RuntimeException("GLEW not initialized")
        }
        if (initialEnvironment.screenEncodingMode == FramebufferCapturer.ZEROCOPY) {
            FramebufferCapturer.initializeZeroCopy(
                initialEnvironment.imageSizeX,
                initialEnvironment.imageSizeY,
                client.framebuffer.colorAttachment,
                client.framebuffer.depthAttachment,
                initialEnvironment.pythonPid,
            )
            csvLogger.log("Initialized zerocopy")
        }
        val buffer = client.framebuffer
        val imageByteString1: ByteString
        val imageByteString2: ByteString
            if (initialEnvironment.eyeDistance > 0) {
            render(client)
            imageByteString1 = FramebufferCapturer.captureFramebuffer(
                buffer.colorAttachment, buffer.fbo, 1, 1, 1, 1, 0, false, false, 0, 0,
            )
            render(client)
            imageByteString2 = FramebufferCapturer.captureFramebuffer(
                buffer.colorAttachment, buffer.fbo, 1, 1, 1, 1, 0, false, false, 0, 0,
            )
        } else {
            imageByteString1 = FramebufferCapturer.captureFramebuffer(
                buffer.colorAttachment, buffer.fbo, 1, 1, 1, 1, 0, false, false, 0, 0,
            )
            imageByteString2 = ByteString.EMPTY
        }
        val observationSpaceMessage = observationSpaceMessage {
            image = imageByteString1
            image2 = imageByteString2
        }
            if (ioPhase == IOPhase.GOT_INITIAL_ENVIRONMENT_SHOULD_SEND_OBSERVATION) {
                ioPhase = IOPhase.GOT_INITIAL_ENVIRONMENT_SENT_OBSERVATION_SKIP_SEND_OBSERVATION
            } else if (ioPhase == IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION) {
                ioPhase = IOPhase.SENT_OBSERVATION_SHOULD_READ_ACTION
            }
            messageIO.writeObservation(observationSpaceMessage)
    }

}
"""
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/FramebufferCapturer.kt": (
        b"""package com.kyhsgeekcode.minecraftenv

object FramebufferCapturer {
    fun captureFramebuffer(
        encodingMode: Int,
    ): ByteString {
        if (encodingMode == ZEROCOPY) {
            return captureFramebufferZerocopyImpl(
            ) ?: ByteString.EMPTY
        } else {
            return captureFramebufferImpl(
            )
        }
    }
}
"""
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/RenderMixin.java": (
        b"class RenderMixin { void upstreamRenderPath() {} }\n"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/WindowOffScreenMixin.java": (
        b"class WindowOffScreenMixin { void upstreamVisibleWindow() {} }\n"
    ),
    "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/TickSpeedMixin.java": (
        b"class TickSpeedMixin {}\n"
    ),
    MIXIN_PATH.as_posix(): json.dumps(
        {
            "required": True,
            "client": [
                "RenderTickCounterAccessor",
                "TickSpeedMixin",
                "WindowOffScreenMixin",
            ],
        },
        indent=2,
    ).encode("utf-8"),
    "build.gradle": (
        b"plugins { id 'fabric-loom' }\n"
        b"def cmakeArgs = ['cmake', '-DCMAKE_POLICY_VERSION_MINIMUM=3.5']\n"
        b"    cmakeArgs += 'src/main/cpp'\n"
    ),
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_source(root: Path) -> dict[str, str]:
    # Scheduling patches target the locked real startup/cleanup layout, not a miniature registerHooks stub.
    # Other fixture files remain small because these sandbox tests do not compile a fake game.
    files = dict(FIXTURE_FILES)
    env_relative = "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
    runtime = Path(__file__).resolve().parents[1] / ".venv/Lib/site-packages/craftground_runtime_mc121"
    files[env_relative] = (runtime / env_relative).read_bytes()
    for relative, value in files.items():
        target = root / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    (root / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    (root / "native-lib.dll").write_bytes(b"native")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    for generated in (
        "run",
        ".gradle",
        ".kotlin",
        "build",
        "__pycache__",
        "_deps",
        "CMakeFiles",
        "ALL_BUILD.dir",
        "native-lib.dir",
        "ZERO_CHECK.dir",
        "Debug",
        "Release",
        "x64",
    ):
        directory = root / generated
        directory.mkdir()
        (directory / "sentinel.bin").write_bytes(b"generated")
    for generated in (
        "CMakeCache.txt",
        "cmake_install.cmake",
        "ALL_BUILD.vcxproj",
        "ALL_BUILD.vcxproj.filters",
        "framebuffer_capturer.sln",
        "native-lib.vcxproj",
        "native-lib.vcxproj.filters",
        "ZERO_CHECK.vcxproj",
        "ZERO_CHECK.vcxproj.filters",
    ):
        (root / generated).write_text("generated\n", encoding="utf-8")
    (root / "latest.log").write_text("generated log\n", encoding="utf-8")
    return {relative: _sha256(value) for relative, value in files.items()}


class RuntimeSandboxTests(unittest.TestCase):
    def test_v3_shared_sources_are_copied_and_fingerprinted(self):
        prepared = prepare_runtime_sandbox(source_root=self.source, sandbox_parent=self.destination,
            sandbox_id="block-v3", clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            expected_fingerprints=self.expected)
        for name in ("ClientBlockObservationV3.java", "ClientObservationRequestV3.java"):
            relative = "src/main/java/com/mc2p/observation/" + name
            self.assertTrue((prepared.path / relative).is_file(), name)
            self.assertEqual((prepared.path / relative).read_bytes(), (OBSERVATION_OVERLAY_ROOT / name).read_bytes())
            self.assertEqual(dict(prepared.manifest.output_fingerprints)[relative],
                             _sha256((OBSERVATION_OVERLAY_ROOT / name).read_bytes()))

    def test_structured_sandbox_installs_shared_observation_sources_and_bridge(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="observation-v2",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        for relative in (
            CLIENT_OBSERVATION_COLLECTOR_RELATIVE_PATH,
            CLIENT_OBSERVATION_JSON_RELATIVE_PATH,
            CLIENT_OBSERVATION_BRIDGE_RELATIVE_PATH,
        ):
            self.assertTrue((prepared.path / relative).is_file(), relative)
            self.assertIn(relative, dict(prepared.manifest.output_fingerprints))
        minecraft_env = (prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt").read_text("utf-8")
        self.assertEqual(minecraft_env.count("ClientObservationCraftGroundBridge.collect("), 1)
        self.assertEqual(minecraft_env.count("ClientObservationCraftGroundBridge.attach("), 1)
        self.assertIn("messageIO.writeObservation(mc2pObservationSpaceMessage)", minecraft_env)

    def test_time_diagnostics_use_an_explicit_shared_kotlin_import(self) -> None:
        prepared = prepare_runtime_sandbox(source_root=self.source, sandbox_parent=self.destination,
            sandbox_id="shared-time", clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            expected_fingerprints=self.expected)
        source = (prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt").read_text("utf-8")
        self.assertEqual(source.count("import com.mc2p.diagnostics.ClientTimeDiagnostics\n"), 1)
        self.assertEqual(source.count("            ClientTimeDiagnostics.observation(client, mc2pGeneration)"), 1)
        self.assertNotIn("            com.mc2p.diagnostics.ClientTimeDiagnostics.observation", source)
        for name in ("ClientTimeDiagnostics.java", "ClientTimeTrace.java", "ClientTimeSegmentWriter.java",
                     "ClientMovementDiagnostics.java", "ClientControlDiagnostics.java",
                     "ClientPhysicsTickDiagnostics.java"):
            relative = "src/main/java/com/mc2p/diagnostics/" + name
            self.assertTrue((prepared.path / relative).is_file())
            self.assertIn(relative, dict(prepared.manifest.output_fingerprints))

    def test_shared_observation_sources_have_no_privileged_or_image_dependencies(self) -> None:
        source = "\n".join(
            (OBSERVATION_OVERLAY_ROOT / name).read_text("utf-8")
            for name in ("ClientObservationCollector.java", "ClientObservationJson.java")
        )
        forbidden = (
            "getServer(",
            "net.minecraft.server",
            "surroundingEntities",
            "surroundingBlocks",
            "EntityRenderListener",
            "getUuid",
            "getNbt",
            "Framebuffer",
            "NativeImage",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)
        json_source = (OBSERVATION_OVERLAY_ROOT / "ClientObservationJson.java").read_text("utf-8")
        self.assertIn("serializeNulls()", json_source)

    def test_directory_publish_retries_transient_windows_permission_error(self) -> None:
        sleep = Mock()
        with patch.object(
            Path,
            "rename",
            side_effect=(PermissionError("scanner still holds a handle"), Path("done")),
        ) as rename:
            _rename_directory_with_retry(
                Path("staging"),
                Path("published"),
                attempts=2,
                delay_seconds=0.05,
                sleep=sleep,
            )

        self.assertEqual(rename.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.destination = self.root / "sandboxes"
        self.source.mkdir()
        self.expected = _write_source(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accelerated_copy_preserves_inputs_and_excludes_generated_state(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="accelerated-a",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        copied = json.loads((prepared.path / MIXIN_PATH).read_text("utf-8"))
        self.assertIn("TickSpeedMixin", copied["client"])
        self.assertTrue((prepared.path / "native-lib.dll").is_file())
        self.assertTrue((prepared.path / "README.md").is_file())
        for generated in (
            "run",
            ".gradle",
            ".kotlin",
            "build",
            "__pycache__",
            "_deps",
            "CMakeFiles",
            "ALL_BUILD.dir",
            "native-lib.dir",
            "ZERO_CHECK.dir",
            "Debug",
            "Release",
            "x64",
        ):
            self.assertFalse((prepared.path / generated).exists())
        for generated in (
            "CMakeCache.txt",
            "cmake_install.cmake",
            "ALL_BUILD.vcxproj",
            "ALL_BUILD.vcxproj.filters",
            "framebuffer_capturer.sln",
            "native-lib.vcxproj",
            "native-lib.vcxproj.filters",
            "ZERO_CHECK.vcxproj",
            "ZERO_CHECK.vcxproj.filters",
        ):
            self.assertFalse((prepared.path / generated).exists())
        self.assertFalse((prepared.path / "latest.log").exists())
        self.assertFalse(prepared.reused)

    def test_reference_copy_removes_only_tick_speed_mixin(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="reference-a",
            clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            expected_fingerprints=self.expected,
        )

        copied = json.loads((prepared.path / MIXIN_PATH).read_text("utf-8"))
        self.assertEqual(
            copied["client"],
            [
                "RenderTickCounterAccessor",
                "WindowOffScreenMixin",
                "GameRendererMixin",
                "BehaviorKeyboardMixin",
                "BehaviorMouseMixin",
                "HandledScreenRenderMixin",
                "BehaviorPlayerMixin",
                "BehaviorInputMixin",
                "ScreenHandlerPropertiesMixin",
                "ClientClockTickMixin",
                "ClientClockWorldMixin",
                "ClientClockPacketMixin",
            ],
        )
        self.assertTrue(
            (
                prepared.path
                / "src/main/java/com/kyhsgeekcode/minecraftenv/mixin/TickSpeedMixin.java"
            ).is_file()
        )
        self.assertEqual(
            prepared.manifest.patch_operations[0],
            "remove client mixin TickSpeedMixin",
        )
        self.assertIn(
            "install structured observation overlay",
            prepared.manifest.patch_operations,
        )

    def test_structured_sandbox_guards_capture_and_installs_headless_overlays(
        self,
    ) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="structured-runtime",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            expected_fingerprints=self.expected,
        )
        java_root = prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv"
        minecraft_env = (java_root / "MinecraftEnv.kt").read_text("utf-8")
        framebuffer = (java_root / "FramebufferCapturer.kt").read_text("utf-8")
        initializer = (java_root / "EnvironmentInitializer.kt").read_text("utf-8")
        render_mixin = (java_root / "mixin/RenderMixin.java").read_text("utf-8")
        game_renderer_mixin = (
            java_root / "mixin/GameRendererMixin.java"
        ).read_text("utf-8")
        window_mixin = (java_root / "mixin/WindowOffScreenMixin.java").read_text(
            "utf-8"
        )
        mixins = json.loads((prepared.path / MIXIN_PATH).read_text("utf-8"))
        build_gradle = (prepared.path / "build.gradle").read_text("utf-8")

        self.assertIn("STRUCTURED_ONLY", minecraft_env)
        self.assertIn("ByteString.EMPTY", minecraft_env)
        self.assertIn(
            "StructuredObservationDiagnostics.recordObservation",
            minecraft_env,
        )
        self.assertIn("if (!structuredOnly &&", minecraft_env)
        self.assertIn("if (structuredOnly)", minecraft_env)
        self.assertLess(
            minecraft_env.index("if (structuredOnly)"),
            minecraft_env.index("else if (initialEnvironment.eyeDistance > 0)"),
        )
        self.assertIn("framebufferCaptureCalls", framebuffer)
        self.assertIn("imageEncodeCalls", framebuffer)
        self.assertNotIn("GameRenderer;render", render_mixin)
        self.assertIn('method = "renderWorld"', game_renderer_mixin)
        self.assertIn('@At("HEAD")', game_renderer_mixin)
        self.assertIn("recordRenderWorldAttempt", game_renderer_mixin)
        self.assertIn("callback.cancel()", game_renderer_mixin)
        self.assertIn('@At("TAIL")', game_renderer_mixin)
        self.assertIn("recordRenderWorldCompletion", game_renderer_mixin)
        self.assertIn("GameRendererMixin", mixins["client"])
        self.assertLess(
            game_renderer_mixin.index("recordRenderWorldAttempt"),
            game_renderer_mixin.index("callback.cancel()"),
        )
        self.assertIn("GLFW_VISIBLE", window_mixin)
        self.assertIn("GLFW_FALSE", window_mixin)
        self.assertIn("glfwCreateWindow", window_mixin)
        self.assertIn("StructuredObservationDiagnostics.recordWindowCreated", window_mixin)
        self.assertNotIn('method = "createWindow", at = @At("HEAD")', window_mixin)
        self.assertIn("GLFW.glfwHideWindow(window.handle)", initializer)
        self.assertNotIn("glfwIconifyWindow", initializer)
        self.assertIn("initialEnvironment.resourceZipPath.isBlank()", initializer)
        self.assertIn("initialEnvironment.mapDirPath.isBlank()", initializer)
        self.assertTrue((java_root / "StructuredObservationDiagnostics.kt").is_file())
        self.assertIn("CRAFTGROUND_GLM_SOURCE", build_gradle)
        self.assertIn("FETCHCONTENT_SOURCE_DIR_GLM", build_gradle)

    def test_sample_clock_sandbox_mutation_or_deletion_is_rejected(self) -> None:
        mode = CraftGroundClockModeV0.REFERENCE_20_TPS
        for mutation in ("changed", "deleted"):
            with self.subTest(mutation=mutation):
                prepared = prepare_runtime_sandbox(
                    source_root=self.source, sandbox_parent=self.destination,
                    sandbox_id="sample-clock-" + mutation, clock_mode=mode,
                    expected_fingerprints=self.expected,
                )
                target = prepared.path / "src/main/java/com/mc2p/observation/ClientSampleClock.java"
                if mutation == "changed":
                    target.write_text("invalid source\n", encoding="utf-8")
                else:
                    target.unlink()
                with self.assertRaises(RuntimePreparationError):
                    validate_sandbox_for_mode(prepared.path, mode)

    def test_name_policy_sources_are_copied_and_guarded_against_mutation(self) -> None:
        mode = CraftGroundClockModeV0.REFERENCE_20_TPS
        for name in ("ClientEntityName.java", "ClientCrosshairAccess.java", "ClientEntityIndex.java"):
            with self.subTest(name=name):
                prepared = prepare_runtime_sandbox(
                    source_root=self.source, sandbox_parent=self.destination,
                    sandbox_id="name-policy-" + name, clock_mode=mode,
                    expected_fingerprints=self.expected,
                )
                target = prepared.path / "src/main/java/com/mc2p/observation" / name
                self.assertTrue(target.is_file(), "shared name policy is absent from runtime")
                target.write_text("invalid source\n", encoding="utf-8")
                with self.assertRaises(RuntimePreparationError):
                    validate_sandbox_for_mode(prepared.path, mode)

    def test_pov_debug_sandbox_preserves_upstream_capture_path(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="pov-debug-runtime",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=self.expected,
        )
        java_root = prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv"

        self.assertEqual(
            (java_root / "MinecraftEnv.kt").read_bytes(),
            (self.source / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt").read_bytes(),
        )
        self.assertEqual(
            (java_root / "FramebufferCapturer.kt").read_bytes(),
            FIXTURE_FILES[
                "src/main/java/com/kyhsgeekcode/minecraftenv/FramebufferCapturer.kt"
            ],
        )
        self.assertFalse((java_root / "StructuredObservationDiagnostics.kt").exists())
        self.assertEqual(prepared.manifest.patch_operations, ())

    def test_lockstep_copy_installs_fingerprinted_repository_overlays(self) -> None:
        mode = getattr(CraftGroundClockModeV0, "LOCKSTEP_ACCELERATED", None)
        self.assertIsNotNone(mode)
        source_before = capture_runtime_source_fingerprints(
            self.source,
            tuple(self.expected),
        )

        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="lockstep",
            clock_mode=mode,
            expected_fingerprints=self.expected,
        )

        self.assertEqual(
            capture_runtime_source_fingerprints(self.source, tuple(self.expected)),
            source_before,
        )
        self.assertIn(
            "install lockstep overlay",
            prepared.manifest.patch_operations,
        )
        java_root = prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv"
        self.assertTrue((java_root / "TickSynchronizer.kt").is_file())
        self.assertTrue((java_root / "LockstepTrace.kt").is_file())
        output_paths = dict(prepared.manifest.output_fingerprints)
        self.assertIn(
            "src/main/java/com/kyhsgeekcode/minecraftenv/LockstepTrace.kt",
            output_paths,
        )

        (java_root / "LockstepTrace.kt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimePreparationError, "output fingerprint"):
            validate_sandbox_for_mode(prepared.path, mode)

    def test_legacy_pov_overlay_keeps_its_explicit_generation_barrier(self) -> None:
        mode = getattr(CraftGroundClockModeV0, "LOCKSTEP_ACCELERATED", None)
        self.assertIsNotNone(mode)
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="lockstep-generation",
            clock_mode=mode,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=self.expected,
        )
        java_root = prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv"
        synchronizer = (java_root / "TickSynchronizer.kt").read_text("utf-8")
        minecraft_env = (java_root / "MinecraftEnv.kt").read_text("utf-8")
        trace = (java_root / "LockstepTrace.kt").read_text("utf-8")

        self.assertIn("data class ActionPermit", synchronizer)
        self.assertIn("fun requestArmAndWait(): ArmCompletion", synchronizer)
        self.assertIn("fun submitActionAndWait(): ActionCompletion", synchronizer)
        self.assertIn("fun beginServerTick(): ServerWork", synchronizer)
        self.assertIn("fun completeServerTick(", synchronizer)
        self.assertIn("awaitNanos", synchronizer)
        self.assertIn("SERVER_WAIT_NANOS", synchronizer)
        self.assertIn("timed out waiting for the next action permit", synchronizer)
        self.assertNotIn("isServerTickCompleted", synchronizer)
        self.assertIn("server.tickManager.setFrozen(true)", minecraft_env)
        self.assertIn("server.sendTimeUpdatePackets()", minecraft_env)
        self.assertIn("server.tickManager.step(1)", minecraft_env)
        self.assertIn("work.generation", minecraft_env)
        self.assertIn("StandardOpenOption.APPEND", trace)
        server_start = minecraft_env.index(
            "ServerTickEvents.START_SERVER_TICK.register("
        )
        server_end = minecraft_env.index(
            "ServerTickEvents.END_SERVER_TICK.register("
        )
        start_hook = minecraft_env[server_start:server_end]
        end_hook = minecraft_env[server_end:]
        self.assertNotIn("beginServerTick()", start_hook)
        self.assertLess(
            end_hook.index("LockstepTrace.record("),
            end_hook.index("completeServerTick("),
        )
        self.assertLess(
            end_hook.index("completeServerTick("),
            end_hook.index("beginServerTick()"),
        )
        self.assertLess(
            end_hook.index("beginServerTick()"),
            end_hook.index("server.tickManager.step(1)"),
        )

    def test_legacy_pov_runtime_keeps_its_fast_reset_hooks(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="lockstep-fast-reset",
            clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=self.expected,
        )
        java_root = prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv"
        synchronizer = (java_root / "TickSynchronizer.kt").read_text("utf-8")
        minecraft_env = (java_root / "MinecraftEnv.kt").read_text("utf-8")

        self.assertIn("RESET", synchronizer)
        self.assertIn("fun requestReset()", synchronizer)
        self.assertIn("tickSynchronizer.requestReset()", minecraft_env)
        self.assertIn("server.tickManager.setFrozen(false)", minecraft_env)
        self.assertIn("lockstepClientPhase = LockstepClientPhase.INITIALIZING", minecraft_env)
        self.assertIn("IOPhase.READ_ACTION_SHOULD_SEND_OBSERVATION", minecraft_env)
        self.assertIn("onLockstepEndWorldTickUnsafe", minecraft_env)

    def test_nonblocking_diagnostics_use_imported_class_in_kotlin_body(self) -> None:
        for mode in (CraftGroundClockModeV0.REFERENCE_20_TPS, CraftGroundClockModeV0.LOCKSTEP_ACCELERATED):
            prepared=prepare_runtime_sandbox(source_root=self.source,sandbox_parent=self.destination,
                sandbox_id='diagnostic-import-'+mode.value,clock_mode=mode,expected_fingerprints=self.expected)
            source=(prepared.path/'src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt').read_text('utf-8')
            self.assertIn('import com.mc2p.diagnostics.ClientTimeDiagnostics',source)
            self.assertIn('        ClientTimeDiagnostics.initialize()',source)
            self.assertIn('            ClientTimeDiagnostics.closeAfter {',source)
            self.assertNotIn('        com.mc2p.diagnostics.',source)

    def test_structured_reset_clears_payload_generation_after_envelope_validation(self) -> None:
        for mode in CraftGroundClockModeV0:
            prepared = prepare_runtime_sandbox(source_root=self.source, sandbox_parent=self.destination,
                sandbox_id=f"payload-reset-{mode.value}", clock_mode=mode, expected_fingerprints=self.expected)
            source = (prepared.path / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt").read_text("utf-8")
            nonblocking = mode in (CraftGroundClockModeV0.REFERENCE_20_TPS, CraftGroundClockModeV0.LOCKSTEP_ACCELERATED)
            boundary = ("val action = mc2pEpoch.unwrap(raw)" if nonblocking
                        else "val action = messageIO.readAction()")
            dispatch = source.split(boundary, 1)[1]
            if nonblocking:
                self.assertIn('val resetsEpisode = action.commandsList.any { it == "fastreset" || it == "fastreset " }', dispatch)
                self.assertIn("if (resetsEpisode) {", dispatch)
            else:
                self.assertIn('if (action.commandsList.any { it == "fastreset" || it == "fastreset " })', dispatch)
            self.assertLess(dispatch.index("validateBeforeDispatch(client, action)"),
                            dispatch.index("mc2pObservationGeneration = -1L"))
            self.assertLess(dispatch.index("mc2pObservationGeneration = -1L"),
                            dispatch.index("applyAction(action, player, client)"))
            if mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
                self.assertLess(dispatch.index("mc2pObservationGeneration = -1L"),
                                dispatch.index("handleCommand(command, client, player)"))
            send = source.split("private fun sendObservation(", 1)[1].split(") {", 1)[1]
            self.assertTrue(send.lstrip().startswith("if (resetPhase != ResetPhase.END_RESET) return"))

    def test_legacy_pov_client_keeps_its_settle_hooks(self) -> None:
        mode = getattr(CraftGroundClockModeV0, "LOCKSTEP_ACCELERATED", None)
        self.assertIsNotNone(mode)
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="lockstep-settle",
            clock_mode=mode,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=self.expected,
        )
        minecraft_env = (
            prepared.path
            / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
        ).read_text("utf-8")

        self.assertIn("enum class LockstepClientPhase", minecraft_env)
        self.assertIn("LockstepClientPhase.SETTLE", minecraft_env)
        settle_start = minecraft_env.index("            LockstepClientPhase.SETTLE -> {")
        settle_end = minecraft_env.index(
            "            LockstepClientPhase.ACTION -> Unit",
            settle_start,
        )
        settle_branch = minecraft_env[settle_start:settle_end]
        self.assertNotIn("applyAction(", settle_branch)
        self.assertIn("return", settle_branch)
        self.assertIn(
            '?: failLockstep("lockstep client baseline is missing")) + 1L',
            minecraft_env,
        )
        self.assertIn(
            "ActionSpaceMessageV2.getDefaultInstance()",
            minecraft_env,
        )
        self.assertNotIn("MAX_LOCKSTEP_SETTLE_CYCLES", minecraft_env)
        self.assertNotIn("lockstepSettleCycles", minecraft_env)
        self.assertIn("LOCKSTEP_SETTLE_TIMEOUT_NANOS = 10_000_000_000L", minecraft_env)
        self.assertIn("lockstepSettleStartedAtNanos = System.nanoTime()", minecraft_env)
        self.assertIn(
            "System.nanoTime() - settleStartedAtNanos > LOCKSTEP_SETTLE_TIMEOUT_NANOS",
            minecraft_env,
        )
        self.assertIn("lockstep client world time overshot", minecraft_env)
        self.assertIn(
            "lockstepSettleTarget = completion.serverWorldTime",
            minecraft_env,
        )
        self.assertIn("ClientLifecycleEvents.CLIENT_STOPPING.register", minecraft_env)
        self.assertIn(
            "work.kind == ServerWorkKind.ARM ||",
            minecraft_env,
        )
        self.assertIn("work.kind == ServerWorkKind.ACTION", minecraft_env)

    def test_unknown_source_fingerprint_is_rejected_before_copy(self) -> None:
        wrong = dict(self.expected)
        wrong["build.gradle"] = "0" * 64

        with self.assertRaisesRegex(RuntimePreparationError, "fingerprint"):
            prepare_runtime_sandbox(
                source_root=self.source,
                sandbox_parent=self.destination,
                sandbox_id="bad-source",
                clock_mode=CraftGroundClockModeV0.ACCELERATED,
                expected_fingerprints=wrong,
            )

        self.assertFalse(self.destination.exists())

    def test_matching_manifest_is_reused_without_rewriting_files(self) -> None:
        first = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="reuse",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )
        marker = first.path / "operator-marker.txt"
        marker.write_text("preserve", encoding="utf-8")

        second = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="reuse",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        self.assertEqual(second.path, first.path)
        self.assertTrue(second.reused)
        self.assertEqual(marker.read_text("utf-8"), "preserve")
        self.assertEqual(
            second.manifest.created_at_utc,
            first.manifest.created_at_utc,
        )

    def test_observation_mode_is_part_of_sandbox_identity(self) -> None:
        first = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="observation-mode",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            expected_fingerprints=self.expected,
        )

        second = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="observation-mode",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=self.expected,
        )

        self.assertFalse(second.reused)
        self.assertEqual(second.path.name, "observation-mode-2")
        self.assertIs(
            first.manifest.observation_mode,
            CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        self.assertIs(
            second.manifest.observation_mode,
            CraftGroundObservationModeV0.POV_DEBUG,
        )

    def test_reuse_rejects_a_manifest_with_mutated_observation_mode(self) -> None:
        first = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="mutated-observation-mode",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            expected_fingerprints=self.expected,
        )
        manifest_path = first.path / "sandbox-manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["observation_mode"] = CraftGroundObservationModeV0.POV_DEBUG.value
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        second = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="mutated-observation-mode",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            expected_fingerprints=self.expected,
        )

        self.assertFalse(second.reused)
        self.assertEqual(second.path.name, "mutated-observation-mode-2")

    def test_lockstep_reuse_rejects_a_changed_repository_patch_recipe(self) -> None:
        recipe_target = (
            "mc2p.backends.craftground_runtime._current_patch_recipe_fingerprint"
        )
        with patch(recipe_target, return_value="1" * 64, create=True):
            first = prepare_runtime_sandbox(
                source_root=self.source,
                sandbox_parent=self.destination,
                sandbox_id="lockstep-recipe",
                clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
                expected_fingerprints=self.expected,
            )
        with patch(recipe_target, return_value="2" * 64, create=True):
            second = prepare_runtime_sandbox(
                source_root=self.source,
                sandbox_parent=self.destination,
                sandbox_id="lockstep-recipe",
                clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
                expected_fingerprints=self.expected,
            )

        self.assertFalse(second.reused)
        self.assertEqual(second.path.name, "lockstep-recipe-2")
        self.assertNotEqual(
            first.manifest.patch_recipe_fingerprint,
            second.manifest.patch_recipe_fingerprint,
        )

    def test_conflicting_directory_gets_unique_sibling_without_overwrite(self) -> None:
        conflict = self.destination / "worker"
        conflict.mkdir(parents=True)
        marker = conflict / "do-not-overwrite.txt"
        marker.write_text("owned", encoding="utf-8")

        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="worker",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        self.assertEqual(prepared.path.name, "worker-2")
        self.assertEqual(marker.read_text("utf-8"), "owned")

    def test_reuse_rejects_manifest_from_different_installed_version(self) -> None:
        first = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="reuse-version",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )
        manifest_path = first.path / "sandbox-manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["craftground_version"] = "0.0.0-stale"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        second = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="reuse-version",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        self.assertFalse(second.reused)
        self.assertEqual(second.path.name, "reuse-version-2")

    def test_manifest_mode_and_output_hash_are_validated(self) -> None:
        prepared = prepare_runtime_sandbox(
            source_root=self.source,
            sandbox_parent=self.destination,
            sandbox_id="validate",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=self.expected,
        )

        loaded = load_sandbox_manifest(prepared.path)
        self.assertEqual(loaded, prepared.manifest)
        self.assertIs(
            loaded.observation_mode,
            CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        with self.assertRaisesRegex(RuntimePreparationError, "clock mode"):
            validate_sandbox_for_mode(
                prepared.path,
                CraftGroundClockModeV0.REFERENCE_20_TPS,
                CraftGroundObservationModeV0.STRUCTURED_ONLY,
            )
        with self.assertRaisesRegex(RuntimePreparationError, "observation mode"):
            validate_sandbox_for_mode(
                prepared.path,
                CraftGroundClockModeV0.ACCELERATED,
                CraftGroundObservationModeV0.POV_DEBUG,
            )

        (prepared.path / MIXIN_PATH).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimePreparationError, "output fingerprint"):
            validate_sandbox_for_mode(
                prepared.path,
                CraftGroundClockModeV0.ACCELERATED,
                CraftGroundObservationModeV0.STRUCTURED_ONLY,
            )

    def test_capture_fingerprints_detects_source_mutation(self) -> None:
        before = capture_runtime_source_fingerprints(
            self.source,
            tuple(self.expected),
        )
        (self.source / "build.gradle").write_text("changed\n", encoding="utf-8")
        after = capture_runtime_source_fingerprints(
            self.source,
            tuple(self.expected),
        )

        self.assertNotEqual(before["build.gradle"], after["build.gradle"])

    def test_unsafe_sandbox_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimePreparationError, "sandbox_id"):
            prepare_runtime_sandbox(
                source_root=self.source,
                sandbox_parent=self.destination,
                sandbox_id="../escape",
                clock_mode=CraftGroundClockModeV0.ACCELERATED,
                expected_fingerprints=self.expected,
            )


if __name__ == "__main__":
    unittest.main()
