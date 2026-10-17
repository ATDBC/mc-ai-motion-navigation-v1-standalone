package com.mc2p.observation;

import com.google.gson.JsonObject;
import java.util.Objects;
import java.util.UUID;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/** Samples collection work, not encoding or server state age. Owned by the client thread. */
public final class ClientSampleClock {
    private final String clockId;
    private final LongSupplier nanoTime;
    private final long origin;

    public ClientSampleClock() {
        this("jvm:" + UUID.randomUUID(), System::nanoTime);
    }

    public ClientSampleClock(String clockId, LongSupplier nanoTime) {
        if (clockId == null || clockId.isBlank()) throw new IllegalArgumentException("clock id missing");
        this.clockId = clockId;
        this.nanoTime = Objects.requireNonNull(nanoTime);
        this.origin = nanoTime.getAsLong();
    }

    public JsonObject sample(Supplier<JsonObject> collect) {
        long started = nanoTime.getAsLong() - origin;
        JsonObject result = Objects.requireNonNull(collect.get());
        long completed = nanoTime.getAsLong() - origin;
        if (started < 0 || completed < started) throw new IllegalStateException("sample clock regressed");
        JsonObject timing = new JsonObject();
        timing.addProperty("clock_id", clockId);
        timing.addProperty("started_at_monotonic_ns", started);
        timing.addProperty("completed_at_monotonic_ns", completed);
        result.add("client_sample", timing);
        return result;
    }

    /** Read-only diagnostic point in exactly the formal sample clock's domain. */
    public record DiagnosticPoint(String clockId, long sampledAtMonotonicNs) {}
    public DiagnosticPoint diagnosticPoint() {
        long sampled = nanoTime.getAsLong() - origin;
        if (sampled < 0) throw new IllegalStateException("diagnostic sample clock regressed");
        return new DiagnosticPoint(clockId, sampled);
    }
}
