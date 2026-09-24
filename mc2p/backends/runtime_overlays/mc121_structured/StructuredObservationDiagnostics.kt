package com.kyhsgeekcode.minecraftenv

import org.lwjgl.glfw.GLFW
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

internal object StructuredObservationDiagnostics {
    const val STRUCTURED_ONLY = true
    const val FILE_NAME = "mc2p-structured-observation.jsonl"

    private val sessionId = UUID.randomUUID().toString()
    private val path = Path.of(FILE_NAME)
    private val renderWorldAttempts = AtomicLong(0L)
    private val renderWorldCompletions = AtomicLong(0L)
    @Volatile
    private var windowVisibleAtCreation: Boolean? = null
    private var observationSequence = 0L

    @JvmStatic
    fun recordWindowCreated(windowHandle: Long) {
        windowVisibleAtCreation =
            windowHandle != 0L &&
                GLFW.glfwGetWindowAttrib(windowHandle, GLFW.GLFW_VISIBLE) == GLFW.GLFW_TRUE
    }

    @JvmStatic
    fun recordRenderWorldAttempt() {
        renderWorldAttempts.incrementAndGet()
    }

    @JvmStatic
    fun recordRenderWorldCompletion() {
        renderWorldCompletions.incrementAndGet()
    }

    @Synchronized
    fun recordObservation(
        windowHandle: Long,
        imageBytes: Int,
        image2Bytes: Int,
        framebufferCaptureCalls: Long,
        imageEncodeCalls: Long,
    ) {
        observationSequence += 1L
        val windowVisible =
            windowHandle != 0L &&
                GLFW.glfwGetWindowAttrib(windowHandle, GLFW.GLFW_VISIBLE) == GLFW.GLFW_TRUE
        val creationVisibilityJson = windowVisibleAtCreation?.toString() ?: "null"
        val worldAttempts = renderWorldAttempts.get()
        val worldCompletions = renderWorldCompletions.get()
        val totalImageBytes = imageBytes.toLong() + image2Bytes.toLong()
        val line =
            "{\"schema_version\":\"mc2p.structured-observation-diagnostics.v2\"," +
                "\"session_id\":\"$sessionId\"," +
                "\"observation_sequence\":$observationSequence," +
                "\"image_bytes\":$totalImageBytes," +
                "\"image_1_bytes\":$imageBytes," +
                "\"image_2_bytes\":$image2Bytes," +
                "\"framebuffer_capture_calls\":$framebufferCaptureCalls," +
                "\"image_encode_calls\":$imageEncodeCalls," +
                "\"window_visible_at_creation\":$creationVisibilityJson," +
                "\"window_visible\":$windowVisible," +
                "\"render_world_attempts\":$worldAttempts," +
                "\"render_world_completions\":$worldCompletions}"
        Files.writeString(
            path,
            "$line\n",
            StandardCharsets.UTF_8,
            StandardOpenOption.CREATE,
            StandardOpenOption.WRITE,
            StandardOpenOption.APPEND,
        )
    }
}
