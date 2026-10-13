package com.mc2p.diagnostics;

import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Path;

/** Read-only action-to-movement timeline sidecar for the D105 F-TICK probe. */
public final class ClientFtickDiagnostics {
    public static final boolean ENABLED = "1".equals(System.getenv("MC2P_FTICK_DIAGNOSTICS"));
    private static final long MAX_EVENTS = 65_536;
    private static ClientTimeSegmentWriter writer;
    private static boolean initialized, closed;
    private static long eventSequence, eventCount, droppedEventCount;

    private ClientFtickDiagnostics() {}

    public static synchronized void initialize(boolean timeEnabled) {
        if (ENABLED && !timeEnabled)
            throw new IllegalStateException("F-TICK diagnostics require explicit time diagnostics");
        if (initialized || closed) return;
        try {
            if (ENABLED) writer = new ClientTimeSegmentWriter(Path.of("ftick-events"), 16 * 1024 * 1024);
            initialized = true;
        } catch (IOException error) { throw new UncheckedIOException(error); }
    }

    public static void frameReceived(String episode, long observationSequence,
                                     long requestSequence, long monotonicNs) {
        event("CLIENT_FRAME_RECEIVED", episode, null, observationSequence,
                requestSequence, null, monotonicNs);
    }

    public static void inputSampled(String episode, long requestSequence,
                                    long movementTick, long monotonicNs) {
        event("INPUT_SAMPLED", episode, null, null, requestSequence,
                movementTick, monotonicNs);
    }

    public static void movementApplied(String episode, long requestSequence,
                                       long movementTick, long monotonicNs) {
        event("MOVEMENT_APPLIED", episode, null, null, requestSequence,
                movementTick, monotonicNs);
    }

    public static void observationSent(String episode, long observationSequence,
                                       long requestSequence, long generation,
                                       Long movementTick, long monotonicNs) {
        event("CLIENT_OBSERVATION_SENT", episode, generation,
                observationSequence, requestSequence, movementTick, monotonicNs);
    }

    private static synchronized void event(
            String kind, String episode, Long generation, Long observationSequence,
            Long requestSequence, Long movementTick, long monotonicNs) {
        if (!ENABLED || closed) return;
        if (!initialized || writer == null)
            throw new IllegalStateException("F-TICK diagnostic boundary is not ready");
        if (monotonicNs < 0) throw new IllegalArgumentException("invalid F-TICK timestamp");
        if (eventCount >= MAX_EVENTS) {
            droppedEventCount++;
            return;
        }
        var row = new JsonObject();
        row.addProperty("schema_version", "mc2p.client-ftick-event.v1");
        row.addProperty("event_sequence", ++eventSequence);
        row.addProperty("kind", kind);
        nullable(row, "episode_id", episode);
        nullable(row, "generation_id", generation);
        nullable(row, "observation_sequence_id", observationSequence);
        nullable(row, "request_sequence_id", requestSequence);
        nullable(row, "movement_tick_id", movementTick);
        row.addProperty("monotonic_ns", monotonicNs);
        writer.accept(row.toString());
        eventCount++;
    }

    private static void nullable(JsonObject row, String key, Object value) {
        if (value == null) row.add(key, JsonNull.INSTANCE);
        else if (value instanceof String text) row.addProperty(key, text);
        else row.addProperty(key, (Long) value);
    }

    public static synchronized void close() {
        if (closed) return;
        closed = true;
        if (writer == null) return;
        var summary = new JsonObject();
        summary.addProperty("schema_version", "mc2p.client-ftick-summary.v1");
        summary.addProperty("kind", "SUMMARY");
        summary.addProperty("event_count", eventCount);
        summary.addProperty("dropped_event_count", droppedEventCount);
        writer.accept(summary.toString());
        try { writer.close(); }
        catch (IOException error) { throw new UncheckedIOException(error); }
    }
}
