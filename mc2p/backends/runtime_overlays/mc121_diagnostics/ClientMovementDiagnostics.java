package com.mc2p.diagnostics;

import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Path;
import java.util.Locale;
import net.minecraft.client.MinecraftClient;
import com.mc2p.observation.ClientObservationCollector;

/** Opt-in actual self-motion evidence only. Never an actor input or a movement setter. */
public final class ClientMovementDiagnostics {
    public static final boolean ENABLED = "1".equals(System.getenv("MC2P_MOVEMENT_DIAGNOSTICS"));
    private static ClientTimeSegmentWriter writer;
    private static boolean initialized, closed;
    private static long sequence;
    private ClientMovementDiagnostics() {}
    public static synchronized void initialize(boolean timeEnabled) {
        if (ENABLED && !timeEnabled) throw new IllegalStateException("movement diagnostics require explicit time diagnostics");
        if (initialized || closed) return;
        try {
            if (ENABLED) writer = new ClientTimeSegmentWriter(Path.of("movement-events"),16*1024*1024);
            initialized = true;
        } catch (IOException error) { throw new UncheckedIOException(error); }
    }
    public static void observation(MinecraftClient client, long generation, ClientTimeTrace.SampleIdentity identity) {
        if (!ENABLED || closed) return;
        if (!initialized || writer == null || !client.isOnThread() || identity.worldId() == null)
            throw new IllegalStateException("movement diagnostic boundary is not ready");
        var row = new JsonObject();
        row.addProperty("schema_version","mc2p.client-movement-event.v2");
        row.addProperty("session_id",identity.sessionId()); row.addProperty("world_id",identity.worldId());
        row.addProperty("event_sequence",++sequence); row.addProperty("time_event_sequence",identity.eventSequence());
        row.addProperty("generation_id",generation); row.addProperty("client_ticks",identity.clientTicks());
        var sample = ClientObservationCollector.diagnosticSampleClock();
        row.addProperty("clock_id",sample.clockId());
        row.addProperty("sampled_at_monotonic_ns",sample.sampledAtMonotonicNs());
        var player = client.player;
        row.addProperty("available",player != null);
        if (player == null) {
            for (String key : new String[]{"actual_sprinting","actual_sneaking","on_ground","pose","velocity"})
                row.add(key,JsonNull.INSTANCE);
        } else {
            row.addProperty("actual_sprinting",player.isSprinting());
            row.addProperty("actual_sneaking",player.isSneaking());
            row.addProperty("on_ground",player.isOnGround());
            row.addProperty("pose",player.getPose().name().toLowerCase(Locale.ROOT));
            var value = player.getVelocity();
            if (!Double.isFinite(value.x) || !Double.isFinite(value.y) || !Double.isFinite(value.z))
                throw new IllegalStateException("nonfinite actual movement");
            var velocity = new JsonObject();
            velocity.addProperty("x",value.x); velocity.addProperty("y",value.y); velocity.addProperty("z",value.z);
            row.add("velocity",velocity);
        }
        writer.accept(row.toString());
    }
    public static synchronized void close() {
        if (closed) return;
        closed = true;
        if (writer != null) try { writer.close(); }
        catch (IOException error) { throw new UncheckedIOException(error); }
    }
}
