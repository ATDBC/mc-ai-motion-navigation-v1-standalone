package com.mc2p.diagnostics;

import com.google.gson.JsonObject;
import java.util.UUID;
import java.util.function.Consumer;

/** Diagnostic event serialization only: no game state API and no actor payload. */
public final class ClientTimeTrace {
    private final String session = UUID.randomUUID().toString();
    private final Consumer<String> sink;
    private long sequence, clientTicks, worldTicks;
    private Object currentWorld;
    private String worldId;

    public ClientTimeTrace(Consumer<String> sink) { this.sink = sink; }
    public record SampleIdentity(String sessionId, String worldId, long eventSequence, long clientTicks) {}
    public SampleIdentity sampleIdentity() { return new SampleIdentity(session,worldId,sequence,clientTicks); }
    public void clientTick() { clientTicks++; }

    private JsonObject event(String kind, Object world) {
        if (world == null) throw new IllegalArgumentException("missing diagnostic world");
        if (world != currentWorld) {
            currentWorld = world;
            worldId = UUID.randomUUID().toString();
        }
        var row = new JsonObject();
        row.addProperty("schema_version", "mc2p.client-time-event.v1");
        row.addProperty("session_id", session);
        row.addProperty("event_sequence", ++sequence);
        row.addProperty("event", kind);
        row.addProperty("world_id", worldId);
        row.addProperty("client_ticks", clientTicks);
        row.addProperty("world_ticks", worldTicks);
        return row;
    }

    public void worldTick(Object world, long before, long after) {
        worldTicks++;
        var row = event("world_tick", world);
        row.addProperty("before", before);
        row.addProperty("after", after);
        sink.accept(row.toString());
    }
    public void packet(Object world, long before, long packetTime, long after) {
        var row = event("time_packet", world);
        row.addProperty("before", before);
        row.addProperty("packet_time", packetTime);
        row.addProperty("after", after);
        sink.accept(row.toString());
    }
    public void observation(Object world, long generation, long time) {
        var row = event("observation", world);
        row.addProperty("generation_id", generation);
        row.addProperty("world_time", time);
        sink.accept(row.toString());
    }
}
