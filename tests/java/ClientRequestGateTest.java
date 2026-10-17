import com.mc2p.actions.ClientRequestGate;

public final class ClientRequestGateTest {
    private static void equal(Object actual, Object expected) {
        if (!java.util.Objects.equals(actual, expected))
            throw new AssertionError("expected=" + expected + " actual=" + actual);
    }
    public static void main(String[] args) throws Exception {
        equal(ClientRequestGate.remainingBudget(100, 200, 150), 50L);
        equal(ClientRequestGate.remainingBudget(100, 200, 190), 10L);
        equal(ClientRequestGate.remainingBudget(100, 200, 200), 0L);
        equal(ClientRequestGate.remainingBudget(100, 200, 201), 0L);
        equal(ClientRequestGate.remainingBudget(100, 200, -1), 0L);
        equal(ClientRequestGate.remainingBudget(100, 200, 10), 100L);
        ClientRequestGate gate = new ClientRequestGate();
        equal(gate.admit("ep", 0, 0, 0, 100, 2, 10), null);
        equal(gate.admit("ep", 0, 0, 0, 100, 2, 11), "duplicate_request");
        equal(gate.admit("other", 1, 1, 1, 100, 2, 12), "episode_mismatch");
        equal(gate.admit("ep", 1, 0, 1, 100, 2, 13), "stale_observation");
        equal(gate.admit("ep", 1, 1, 1, 100, 2, 14), "duplicate_request");
        equal(gate.admit("ep", 2, 1, 1, 0, 2, 15), "deadline_exceeded");
        equal(gate.admit("ep", 3, 1, 1, 100, 2, 20), null);
        equal(gate.leaseActive(21), true);
        gate.endControlTick();
        equal(gate.leaseActive(22), true);
        gate.endControlTick();
        equal(gate.leaseActive(23), false);
        equal(gate.admit("ep", 4, 2, 2, 10, 20, 30), null);
        equal(gate.leaseActive(40), false);
        // A delayed cancellation may not release a newer owner's controls.
        equal(gate.admit("ep", 5, 3, 3, 100, 20, 50), null);
        equal(gate.cancel(4), false);
        equal(gate.leaseActive(51), true);
        equal(gate.cancel(5), true);
        equal(gate.leaseActive(52), false);
        Throwable[] failure = new Throwable[1];
        Thread thread = new Thread(() -> {
            try { gate.reset(); } catch (Throwable error) { failure[0] = error; }
        });
        thread.start(); thread.join();
        equal(failure[0] instanceof IllegalStateException, true);
        gate.reset();
        equal(gate.admit("new-episode", 0, 0, 0, 100, 1, 70), null);
        System.out.println("CLIENT_REQUEST_GATE_OK");
    }
}
