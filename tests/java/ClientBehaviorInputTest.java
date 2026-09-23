import com.mc2p.actions.ClientBehaviorInput;
import com.mc2p.actions.ClientRequestGate;

public final class ClientBehaviorInputTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {1}; boolean[] allowed = {true};
        ClientBehaviorInput input = new ClientBehaviorInput(gate, () -> now[0], () -> allowed[0]);
        gate.admit("ep", 0, 0, 0, 100, 2, now[0]);
        input.set(1, -1, true, false, true);
        input.tick(false, 0.3f);
        yes(input.movementForward == 1 && input.movementSideways == -1 && input.jumping);
        yes(input.pressingForward && input.pressingRight && input.sprintRequested());
        yes(input.lastSample() != null);
        yes(input.lastSample().forward() == 1 && input.lastSample().strafe() == -1);
        yes("leased".equals(input.lastSample().state()));
        input.tick(true, 0.3f);
        yes(input.movementForward == 0.3f && input.movementSideways == -0.3f);
        input.tick(false, 0.3f);
        yes(input.movementForward == 0 && !input.jumping && !input.sprintRequested());
        yes(input.sampleCount() == 3 && input.leasedSampleCount() == 2);
        gate.admit("ep", 1, 0, 0, 100, 20, now[0]);
        input.set(1, 0, false, true, false);
        now[0] = 101;
        input.tick(false, 1);
        yes(input.movementForward == 0 && !input.sneaking);
        gate.admit("ep", 2, 0, 0, 100, 20, now[0]);
        input.set(1, 0, false, false, true);
        allowed[0] = false;
        input.tick(false, 1);
        yes(input.movementForward == 0);
        allowed[0] = true; // Leaving a GUI cannot revive held controls.
        input.tick(false, 1);
        yes(input.movementForward == 0);
        input.set(1, 0, false, false, true);
        gate.cancel(2);
        input.tick(false, 1);
        yes(input.movementForward == 0);
        gate.admit("ep", 3, 0, 0, 100, 20, now[0]);
        input.set(1, 0, false, false, false);
        input.clear();
        input.tick(false, 1);
        yes(input.movementForward == 0 && !input.pressingForward);
        gate.admit("ep", 4, 0, 0, 100, 20, now[0]);
        input.set(1, 0, true, true, false);
        input.tick(false, 0.3f);
        input.beginSnapshot();
        // Vanilla reads last sample BEFORE Input.tick to decide crouch/edge transitions.
        yes(input.sneaking && input.jumping && input.movementForward == 1);
        // Rejection after admission must not renew the old desired controls.
        input.tick(true, 0.3f);
        yes(!input.sneaking && !input.jumping && input.movementForward == 0);
        clientTickExpiryWithoutPlayerSampling();
        singleTickSneakSurvivesUnleasedSamplingGaps();
        sneakProjectionDoesNotGrantOrRenewControls();
        diagnosticCallbackReportsActualConsumptionWithoutChangingLease();
        operationSpecificRejectionDoesNotBindDiagnosticIdentity();
        boundedLedgerDropsOldestSamplesWithoutCrashing();
        System.out.println("CLIENT_BEHAVIOR_INPUT_OK");
    }

    private static void boundedLedgerDropsOldestSamplesWithoutCrashing() {
        ClientRequestGate gate = new ClientRequestGate();
        ClientBehaviorInput input = new ClientBehaviorInput(gate, () -> 1L, () -> true);
        for (int index = 0; index < 70; index++) input.tick(false, 1);
        yes(input.oldestRetainedMovementTick() == 7);
        var retained = input.drainSamples();
        yes(retained.size() == 64);
        yes(retained.get(0).movementTickId() == 7);
        yes(retained.get(63).movementTickId() == 70);
        yes(input.droppedSampleCount() == 6);
        yes(input.oldestRetainedMovementTick() == 0);
    }

    private static void singleTickSneakSurvivesUnleasedSamplingGaps() {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {1};
        ClientBehaviorInput input = new ClientBehaviorInput(gate, () -> now[0], () -> true);
        // In the standing, clear-space case vanilla reads Input.sneaking before
        // Input.tick to compute crouching/slowDown. The real game probe covers
        // that native pose calculation; this exercises our input phase boundary.
        for (int sequence = 0; sequence < 3; sequence++) {
            yes(gate.admit("cadence", sequence, sequence, sequence, 100, 1, now[0]) == null);
            input.beginSnapshot();
            input.set(1, -1, true, true, false);
            yes(!input.jumping && !input.sneaking);
            input.prepareForPlayerTick();
            yes(!input.jumping && input.movementForward == 0); // No premature jump edge or movement.
            float factor = sequence == 2 ? 0.6f : 0.3f; // Use the supplied vanilla attribute, not a baked constant.
            input.tick(input.sneaking, factor);
            if (input.movementForward != factor || input.movementSideways != -factor)
                throw new AssertionError("single-tick sneak was not projected before vanilla slowdown");
            yes(input.jumping && !gate.leaseActive(now[0]));
            now[0]++;
            input.prepareForPlayerTick();
            yes(input.jumping && input.sneaking); // Logical exhaustion preserves vanilla's last edges.
            input.tick(input.sneaking, factor); // The intervening unleased sample remains neutral.
            yes(input.movementForward == 0 && !input.jumping && !input.sneaking);
            yes(input.sampleCount() == (sequence + 1) * 2 && input.leasedSampleCount() == sequence + 1);
            now[0]++;
        }
    }

    private static void sneakProjectionDoesNotGrantOrRenewControls() {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {1}; boolean[] allowed = {true};
        ClientBehaviorInput input = new ClientBehaviorInput(gate, () -> now[0], () -> allowed[0]);
        input.set(1, 0, true, true, false);
        input.prepareForPlayerTick(); // No admitted request.
        yes(!input.sneaking && !input.jumping && input.sampleCount() == 0);
        yes(gate.admit("projection", 0, 0, 0, 100, 1, now[0]) == null);
        allowed[0] = false;
        input.prepareForPlayerTick();
        yes(!input.sneaking); // GUI/death/world guard remains authoritative.
        allowed[0] = true;
        gate.cancel(0);
        input.prepareForPlayerTick();
        yes(!input.sneaking);
        yes(gate.admit("projection", 1, 1, 1, 100, 1, now[0]) == null);
        now[0] = 101;
        input.prepareForPlayerTick();
        yes(!input.sneaking); // Wall deadline cannot be renewed by phase preparation.
        yes(gate.admit("projection", 2, 2, 2, 100, 1, now[0]) == null);
        input.beginSnapshot(); // Neutral or rejected snapshot clears desired sneak.
        input.prepareForPlayerTick();
        yes(!input.sneaking && input.sampleCount() == 0 && input.leasedSampleCount() == 0);
        input.set(1, 0, false, false, false);
        input.prepareForPlayerTick();
        input.tick(true, 0.6f); // Non-sneak crawling/low-ceiling slowdown is still vanilla's decision.
        yes(input.movementForward == 0.6f && !input.sneaking);
    }

    private static void clientTickExpiryWithoutPlayerSampling() {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {10}; boolean[] allowed = {true};
        ClientBehaviorInput input = new ClientBehaviorInput(gate, () -> now[0], () -> allowed[0]);
        yes(gate.admit("expiry", 0, 0, 0, 100, 1, now[0]) == null);
        input.set(1, -1, true, true, true);
        input.tick(false, 1);
        yes(!gate.leaseActive(11)); // The logical lease is consumed, but vanilla still needs its edge.
        now[0] = 109;
        input.expireOnClientTick();
        yes(input.jumping && input.sneaking && input.sprintRequested() && input.movementForward == 1);
        yes(input.sampleCount() == 1 && input.leasedSampleCount() == 1);
        now[0] = 110;
        input.expireOnClientTick(); // No new request, no Input.tick: wall expiry must still clear.
        yes(input.movementForward == 0 && input.movementSideways == 0);
        yes(!input.jumping && !input.sneaking && !input.sprintRequested() && !input.pressingForward);
        yes(input.sampleCount() == 1 && input.leasedSampleCount() == 1);
        yes("duplicate_request".equals(gate.admit("expiry", 0, 0, 0, 100, 20, 110)));
        yes("episode_mismatch".equals(gate.admit("old-episode", 1, 0, 0, 100, 20, 110)));
        yes(gate.admit("expiry", 1, 0, 0, 100, 20, 110) == null);
        input.set(1, 0, true, false, true);
        input.tick(false, 1);
        allowed[0] = false;
        input.expireOnClientTick(); // GUI/context loss cannot wait for a player simulation tick.
        yes(input.movementForward == 0 && !input.jumping && !input.sprintRequested());
        yes(input.sampleCount() == 2 && input.leasedSampleCount() == 2);
        allowed[0] = true;
        input.expireOnClientTick();
        input.tick(false, 1);
        yes(input.movementForward == 0 && !input.jumping); // Closing GUI does not revive intent.
        yes(gate.admit("expiry", 2, 0, 0, 100, 20, 110) == null);
        input.set(1, 0, true, false, true);
        input.tick(false, 1);
        yes(input.movementForward == 1 && input.jumping); // Fresh intent recovers normally.
    }

    private static void diagnosticCallbackReportsActualConsumptionWithoutChangingLease() {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {50};
        java.util.ArrayList<ClientBehaviorInput.Sample> samples = new java.util.ArrayList<>();
        ClientBehaviorInput input = new ClientBehaviorInput(
            gate, () -> now[0], () -> true, samples::add);
        yes(gate.admit("diagnostic", 7, 0, 0, 100, 1, now[0]) == null);
        input.bindAcceptedRequest("diagnostic", 7);
        input.set(1, -1, false, false, false);
        input.tick(true, .3f);
        yes(samples.size() == 1);
        var leased = samples.get(0);
        yes(leased.movementTickId() == 1);
        yes("diagnostic".equals(leased.episodeId()) && leased.requestSequenceId() == 7);
        yes("leased".equals(leased.state()) && leased.sampledAtJvmNs() == 50);
        yes(leased.forward() == .3f && leased.strafe() == -.3f);
        yes(input.leasedSampleCount() == 1 && !gate.leaseActive(50));
        yes(input.drainSamples().size() == 1);
        yes(input.drainSamples().isEmpty());
        now[0] = 51;
        input.tick(false, 1);
        yes(samples.size() == 2);
        var released = samples.get(1);
        yes(released.movementTickId() == 2);
        yes("lease_exhausted".equals(released.state()));
        yes(released.forward() == 0 && released.strafe() == 0);
        yes(released.requestSequenceId() == 7);
        yes(input.leasedSampleCount() == 1);
    }

    private static void operationSpecificRejectionDoesNotBindDiagnosticIdentity() {
        ClientRequestGate gate = new ClientRequestGate();
        long[] now = {70};
        java.util.ArrayList<ClientBehaviorInput.Sample> samples = new java.util.ArrayList<>();
        ClientBehaviorInput input = new ClientBehaviorInput(
            gate, () -> now[0], () -> true, samples::add);
        yes(gate.admit("operation", 11, 0, 0, 100, 1, now[0]) == null);
        input.beginSnapshot();
        input.set(0, 0, false, false, false);
        input.bindDispatchedRequest("operation", 11, "rejected");
        input.tick(false, 1);
        yes(samples.size() == 1 && samples.get(0).episodeId() == null);

        now[0]++;
        yes(gate.admit("operation", 12, 0, 0, 100, 1, now[0]) == null);
        input.beginSnapshot();
        input.set(1, 0, false, false, false);
        input.bindDispatchedRequest("operation", 12, "executed");
        input.tick(false, 1);
        yes(samples.size() == 2);
        yes("operation".equals(samples.get(1).episodeId())
            && samples.get(1).requestSequenceId() == 12);
    }
}
