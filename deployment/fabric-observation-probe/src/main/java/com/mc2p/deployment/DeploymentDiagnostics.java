package com.mc2p.deployment;

import com.google.gson.JsonObject;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.client.MinecraftClient;
import static org.lwjgl.glfw.GLFW.*;

/** Attached method counters only. No image content is ever produced or added to a sample. */
public final class DeploymentDiagnostics {
    public static long clientTicks, worldAttempts, worldCompletions, guiAttempts, guiCompletions;
    private static final AtomicLong captureAttempts = new AtomicLong(), encodeAttempts = new AtomicLong();
    private static boolean windowRecorded, visibleAtCreation;

    public static void windowCreated(long handle) {
        windowRecorded = handle != 0;
        visibleAtCreation |= handle != 0 && glfwGetWindowAttrib(handle, GLFW_VISIBLE) != GLFW_FALSE;
    }
    public static void rejectCapture() {
        captureAttempts.incrementAndGet();
        throw new IllegalStateException("structured_only framebuffer capture forbidden");
    }
    public static void rejectEncoding() {
        encodeAttempts.incrementAndGet();
        throw new IllegalStateException("structured_only image encoding forbidden");
    }
    public static JsonObject sample(MinecraftClient client, String remoteAddress) {
        var result = new JsonObject();
        result.addProperty("schema_version", "mc2p.deployment_diagnostics.v1");
        result.addProperty("client_tick", clientTicks);
        result.addProperty("remote_address", remoteAddress);
        result.addProperty("has_integrated_server", client.getServer() != null);
        result.addProperty("window_recorded", windowRecorded);
        result.addProperty("window_visible_at_creation", visibleAtCreation);
        result.addProperty("window_visible", glfwGetWindowAttrib(client.getWindow().getHandle(), GLFW_VISIBLE) != GLFW_FALSE);
        result.addProperty("world_render_attempts", worldAttempts);
        result.addProperty("world_render_completions", worldCompletions);
        result.addProperty("gui_render_attempts", guiAttempts);
        result.addProperty("gui_render_completions", guiCompletions);
        result.addProperty("framebuffer_capture_attempts", captureAttempts.get());
        result.addProperty("image_encode_attempts", encodeAttempts.get());
        return result;
    }
}
