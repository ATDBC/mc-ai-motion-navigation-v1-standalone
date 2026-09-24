import com.google.gson.JsonObject;
import com.mc2p.observation.ClientSampleClock;
import java.util.concurrent.atomic.AtomicInteger;

public class ClientSampleClockTest {
    public static void main(String[] args) {
        long[] readings = {1000, 1010, 1025, 1030, 1035, 1040};
        AtomicInteger index = new AtomicInteger();
        ClientSampleClock clock = new ClientSampleClock("jvm-test", () -> readings[index.getAndIncrement()]);
        JsonObject result = clock.sample(() -> {
            if (index.get() != 2) throw new AssertionError("start must precede collection");
            JsonObject data = new JsonObject(); data.addProperty("world_time", 62); return data;
        });
        JsonObject sample = result.getAsJsonObject("client_sample");
        if (sample.size() != 3 || !sample.get("clock_id").getAsString().equals("jvm-test")
                || sample.get("started_at_monotonic_ns").getAsLong() != 10
                || sample.get("completed_at_monotonic_ns").getAsLong() != 25
                || result.get("world_time").getAsLong() != 62) throw new AssertionError(result);
        JsonObject reset = clock.sample(JsonObject::new).getAsJsonObject("client_sample");
        if (reset.get("started_at_monotonic_ns").getAsLong() != 30) throw new AssertionError("clock reset");
        var diagnostic = clock.diagnosticPoint();
        if (!diagnostic.clockId().equals("jvm-test") || diagnostic.sampledAtMonotonicNs()!=40)
            throw new AssertionError("diagnostic must use the same relative origin and clock id");
        RuntimeException failure = new RuntimeException("collection failure");
        ClientSampleClock failing = new ClientSampleClock("jvm-error", () -> 1000L);
        try { failing.sample(() -> { throw failure; }); throw new AssertionError("swallowed"); }
        catch (RuntimeException caught) { if (caught != failure) throw new AssertionError(caught); }
        long[] backward = {100, 110, 109}; AtomicInteger j = new AtomicInteger();
        ClientSampleClock bad = new ClientSampleClock("jvm-backward", () -> backward[j.getAndIncrement()]);
        try { bad.sample(JsonObject::new); throw new AssertionError("regression published"); }
        catch (IllegalStateException expected) { }
        System.out.println("CLIENT_SAMPLE_CLOCK_OK");
    }
}
