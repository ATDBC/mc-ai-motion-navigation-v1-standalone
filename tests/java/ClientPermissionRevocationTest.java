import com.mc2p.actions.ClientBehaviorInput;
import com.mc2p.actions.ClientRequestGate;
import java.util.ArrayList;

public final class ClientPermissionRevocationTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }

    public static void main(String[] args) {
        long[] now = {1_000_000_000L};
        boolean[] allowed = {true};
        var samples = new ArrayList<ClientBehaviorInput.Sample>();
        var reasons = new ArrayList<String>();
        var gate = new ClientRequestGate();
        yes(gate.admit("ep", 25, 0, 0, 2_000_000_000L, 1, now[0]) == null);
        var input = new ClientBehaviorInput(gate, () -> now[0], () -> allowed[0], samples::add);
        input.setTimingRejectionObserver(reasons::add);
        input.bindAcceptedRequest("ep", 25);
        input.set(1, 0, false, false, true);
        input.armDefault("ep", 25, 2, 0, 0, false, true, false, 8, 400_000_000L);
        yes(gate.admitPending("ep", 26, 0, 0, 2_000_000_000L, 1) == null);
        input.stageWindowed("ep", 26, 0, 3, 3, now[0] + 2_000_000_000L,
                1, 1, 0, true, false, true, "pending", null);
        allowed[0] = false;
        input.prepareForPlayerTick();
        input.tick(false, 1.0F);
        var events = input.drainTimingEvents();
        var sample = samples.getFirst();
        yes(reasons.equals(java.util.List.of("permission_revoked")));
        yes(events.size() == 2 && events.get(0).status().equals("window_pending")
                && events.get(1).status().equals("window_missed"));
        yes(sample.state().equals("disallowed") && sample.requestSequenceId() == 25);
        yes(input.timingResponseReady());
        System.out.printf("{\"reason\":\"%s\",\"input_samples\":%d,"
                        + "\"sample_tick\":%d,\"sample_sequence\":%d,"
                        + "\"pending_tick\":%d,\"missed_tick\":%d}%n",
                reasons.getFirst(), input.sampleCount(), sample.movementTickId(),
                sample.requestSequenceId(), events.get(0).movementTickId(), events.get(1).movementTickId());
    }
}
