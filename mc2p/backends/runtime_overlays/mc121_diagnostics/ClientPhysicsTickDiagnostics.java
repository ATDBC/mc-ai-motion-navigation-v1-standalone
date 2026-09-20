package com.mc2p.diagnostics;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Path;
import java.util.Locale;
import net.minecraft.client.MinecraftClient;

/**
 * Opt-in evidence that joins one vanilla tickMovement call across its real phases.
 * This sidecar is diagnostic-only and never feeds actor observations or control.
 */
public final class ClientPhysicsTickDiagnostics {
    public static final boolean ENABLED = "1".equals(System.getenv("MC2P_PHYSICS_TICK_DIAGNOSTICS"));
    private static ClientTimeSegmentWriter writer;
    private static boolean initialized, closed;
    private static long movementTickId;
    private static ActiveTick active;

    private record ActiveTick(JsonObject row, boolean preOnGround) {}

    private ClientPhysicsTickDiagnostics() {}

    public static synchronized void initialize(boolean timeEnabled) {
        if (ENABLED && !timeEnabled)
            throw new IllegalStateException("physics tick diagnostics require explicit time diagnostics");
        if (initialized || closed) return;
        try {
            if (ENABLED) writer = new ClientTimeSegmentWriter(Path.of("physics-tick-events"), 16 * 1024 * 1024);
            initialized = true;
        } catch (IOException error) { throw new UncheckedIOException(error); }
    }

    public static void beforeMovement(MinecraftClient client) {
        if (!ENABLED || closed) return;
        requireReady(client);
        if (active != null) throw new IllegalStateException("previous movement tick is incomplete");
        if (client.player == null) throw new IllegalStateException("movement tick has no player");
        var identity = ClientTimeDiagnostics.controlIdentity();
        if (identity.worldId() == null) throw new IllegalStateException("movement tick has no world identity");
        var row = new JsonObject();
        row.addProperty("schema_version", "mc2p.client-physics-tick.v1");
        row.addProperty("session_id", identity.sessionId());
        row.addProperty("world_id", identity.worldId());
        row.addProperty("movement_tick_id", ++movementTickId);
        row.addProperty("time_event_sequence", identity.eventSequence());
        row.addProperty("client_ticks", identity.clientTicks());
        row.add("pre_state", state(client));
        active = new ActiveTick(row, client.player.isOnGround());
    }

    public static void afterInputSample(MinecraftClient client, String episode, long requestSequence,
                                        long sampledAtJvmNs, String inputState,
                                        float forward, float strafe, boolean jump,
                                        boolean sneak, boolean sprint) {
        if (!ENABLED || closed) return;
        requireReady(client);
        if (active == null || active.row().has("actual_input"))
            throw new IllegalStateException("movement input phase is out of order");
        if (sampledAtJvmNs < 0 || !Float.isFinite(forward) || !Float.isFinite(strafe))
            throw new IllegalArgumentException("movement input sample is invalid");
        var row = active.row();
        if (episode == null) row.add("episode_id", JsonNull.INSTANCE);
        else row.addProperty("episode_id", episode);
        if (requestSequence < 0) row.add("request_sequence_id", JsonNull.INSTANCE);
        else row.addProperty("request_sequence_id", requestSequence);
        row.addProperty("sampled_at_jvm_ns", sampledAtJvmNs);
        row.addProperty("input_state", inputState);
        row.addProperty("movement_yaw", client.player.getYaw());
        row.addProperty("movement_pitch", client.player.getPitch());
        var input = new JsonObject();
        input.addProperty("forward", forward); input.addProperty("strafe", strafe);
        input.addProperty("jump", jump); input.addProperty("sneak", sneak);
        input.addProperty("sprint", sprint);
        row.add("actual_input", input);
    }

    public static void afterMovement(MinecraftClient client) {
        if (!ENABLED || closed) return;
        requireReady(client);
        if (active == null || !active.row().has("actual_input"))
            throw new IllegalStateException("movement completion phase is out of order");
        var row = active.row();
        row.add("post_state", state(client));
        var events = new JsonArray();
        boolean postOnGround = client.player.isOnGround();
        if (active.preOnGround() && !postOnGround) events.add("left_ground");
        if (!active.preOnGround() && postOnGround) events.add("landed");
        if (client.player.horizontalCollision) events.add("horizontal_collision");
        if (client.player.verticalCollision) events.add("vertical_collision");
        row.add("contact_events", events);
        writer.accept(row.toString());
        active = null;
    }

    private static void requireReady(MinecraftClient client) {
        if (!initialized || writer == null || !client.isOnThread())
            throw new IllegalStateException("physics tick diagnostic boundary is not ready");
        if (client.player == null) throw new IllegalStateException("physics tick diagnostic has no player");
    }

    private static JsonObject state(MinecraftClient client) {
        var player = client.player;
        var velocity = player.getVelocity();
        double x = player.getX(), y = player.getY(), z = player.getZ();
        float yaw = player.getYaw(), pitch = player.getPitch();
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z)
                || !Double.isFinite(velocity.x) || !Double.isFinite(velocity.y)
                || !Double.isFinite(velocity.z) || !Float.isFinite(yaw) || !Float.isFinite(pitch))
            throw new IllegalStateException("nonfinite physics tick state");
        var position = new JsonObject();
        position.addProperty("x", x); position.addProperty("y", y); position.addProperty("z", z);
        var velocityJson = new JsonObject();
        velocityJson.addProperty("x", velocity.x); velocityJson.addProperty("y", velocity.y);
        velocityJson.addProperty("z", velocity.z);
        var state = new JsonObject();
        state.add("position", position); state.add("velocity", velocityJson);
        state.addProperty("yaw", yaw); state.addProperty("pitch", pitch);
        state.addProperty("on_ground", player.isOnGround());
        state.addProperty("horizontal_collision", player.horizontalCollision);
        state.addProperty("vertical_collision", player.verticalCollision);
        state.addProperty("actual_sprinting", player.isSprinting());
        state.addProperty("actual_sneaking", player.isSneaking());
        state.addProperty("pose", player.getPose().name().toLowerCase(Locale.ROOT));
        return state;
    }

    public static synchronized void close() {
        if (closed) return;
        closed = true;
        if (active != null) throw new IllegalStateException("cannot seal an incomplete movement tick");
        if (writer != null) try { writer.close(); }
        catch (IOException error) { throw new UncheckedIOException(error); }
    }
}
