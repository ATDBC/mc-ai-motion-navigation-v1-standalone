package com.mc2p.diagnostics;

import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Path;
import java.util.Locale;
import net.minecraft.client.MinecraftClient;

/** Opt-in actual look/input stage evidence. Never participates in behavior decisions. */
public final class ClientControlDiagnostics {
    public static final boolean ENABLED = "1".equals(System.getenv("MC2P_CONTROL_DIAGNOSTICS"));
    private static ClientTimeSegmentWriter writer;
    private static boolean initialized, closed;
    private static long sequence;

    private ClientControlDiagnostics() {}

    public static synchronized void initialize(boolean timeEnabled) {
        if (ENABLED && !timeEnabled)
            throw new IllegalStateException("control diagnostics require explicit time diagnostics");
        if (initialized || closed) return;
        try {
            if (ENABLED) writer = new ClientTimeSegmentWriter(Path.of("control-events"), 16*1024*1024);
            initialized = true;
        } catch (IOException error) { throw new UncheckedIOException(error); }
    }

    private static JsonObject event(MinecraftClient client, String kind, String episode,
                                    long requestSequence, long sampledAtJvmNs) {
        if (!ENABLED || closed) return null;
        if (!initialized || writer == null || !client.isOnThread())
            throw new IllegalStateException("control diagnostic boundary is not ready");
        var identity = ClientTimeDiagnostics.controlIdentity();
        if (identity.worldId() == null)
            throw new IllegalStateException("control diagnostic has no world identity");
        if (episode == null || requestSequence < 0 || sampledAtJvmNs < 0)
            throw new IllegalArgumentException("control diagnostic request identity is invalid");
        var row = new JsonObject();
        row.addProperty("schema_version", "mc2p.client-control-event.v1");
        row.addProperty("session_id", identity.sessionId());
        row.addProperty("world_id", identity.worldId());
        row.addProperty("event_sequence", ++sequence);
        row.addProperty("time_event_sequence", identity.eventSequence());
        row.addProperty("client_ticks", identity.clientTicks());
        row.addProperty("sampled_at_jvm_ns", sampledAtJvmNs);
        row.addProperty("event", kind);
        row.addProperty("episode_id", episode);
        row.addProperty("request_sequence_id", requestSequence);
        addPose(row, client);
        return row;
    }

    private static void addPose(JsonObject row, MinecraftClient client) {
        var player = client.player;
        row.addProperty("available", player != null);
        if (player == null) {
            row.add("actual_pose", JsonNull.INSTANCE);
            return;
        }
        var velocity = player.getVelocity();
        double x = player.getX(), y = player.getY(), z = player.getZ();
        float yaw = player.getYaw(), pitch = player.getPitch();
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)
                || !Double.isFinite(velocity.x) || !Double.isFinite(velocity.y)
                || !Double.isFinite(velocity.z) || !Float.isFinite(yaw) || !Float.isFinite(pitch))
            throw new IllegalStateException("nonfinite actual control state");
        var position = new JsonObject();
        position.addProperty("x", x); position.addProperty("y", y); position.addProperty("z", z);
        var velocityJson = new JsonObject();
        velocityJson.addProperty("x", velocity.x); velocityJson.addProperty("y", velocity.y);
        velocityJson.addProperty("z", velocity.z);
        var pose = new JsonObject();
        pose.add("position", position); pose.add("velocity", velocityJson);
        pose.addProperty("yaw", yaw); pose.addProperty("pitch", pitch);
        pose.addProperty("on_ground", player.isOnGround());
        pose.addProperty("actual_sprinting", player.isSprinting());
        pose.addProperty("actual_sneaking", player.isSneaking());
        pose.addProperty("pose", player.getPose().name().toLowerCase(Locale.ROOT));
        row.add("actual_pose", pose);
    }

    public static void lookApplied(MinecraftClient client, String episode,
                                   long requestSequence, long sampledAtJvmNs) {
        var row = event(client, "look_applied", episode, requestSequence, sampledAtJvmNs);
        if (row == null) return;
        var look = new JsonObject();
        look.addProperty("yaw", client.player.getYaw());
        look.addProperty("pitch", client.player.getPitch());
        row.add("actual_look", look);
        writer.accept(row.toString());
    }

    public static void attackDispatched(MinecraftClient client, String episode,
                                        long requestSequence, long sampledAtJvmNs,
                                        String entityRef) {
        if (entityRef == null || !entityRef.matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"))
            throw new IllegalArgumentException("attack diagnostic entity reference");
        var row = event(client, "attack_dispatched", episode, requestSequence, sampledAtJvmNs);
        if (row == null) return;
        var operation = new JsonObject();
        operation.addProperty("kind", "attack_entity");
        operation.addProperty("entity_ref", entityRef);
        row.add("actual_operation", operation);
        writer.accept(row.toString());
    }

    public static void inputConsumed(MinecraftClient client, String episode,
                                     long requestSequence, long sampledAtJvmNs,
                                     String state, float forward, float strafe,
                                     boolean jump, boolean sneak, boolean sprint) {
        if (!ENABLED || closed) return;
        if (!java.util.Set.of("leased", "neutral", "lease_exhausted", "expired", "disallowed").contains(state))
            throw new IllegalArgumentException("invalid actual input state");
        var row = event(client, "input_consumed", episode, requestSequence, sampledAtJvmNs);
        var input = new JsonObject();
        input.addProperty("forward", forward); input.addProperty("strafe", strafe);
        input.addProperty("jump", jump); input.addProperty("sneak", sneak);
        input.addProperty("sprint", sprint);
        row.addProperty("input_state", state);
        row.add("actual_input", input);
        writer.accept(row.toString());
    }

    public static synchronized void close() {
        if (closed) return;
        closed = true;
        if (writer != null) try { writer.close(); }
        catch (IOException error) { throw new UncheckedIOException(error); }
    }
}
