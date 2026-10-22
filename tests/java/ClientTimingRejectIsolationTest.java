import com.mc2p.actions.ClientActionRequest;
import com.mc2p.actions.ClientBehaviorInput;
import com.mc2p.actions.ClientRequestGate;
import com.mc2p.actions.ClientTimingAdmission;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;

public final class ClientTimingRejectIsolationTest {
    private static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }

    public static void main(String[] args) {
        long[] now = {1_000_000_000L};
        var gate = new ClientRequestGate();
        yes(gate.admit("ep", 25, 7, 7, 2_000_000_000L, 1, now[0]) == null);
        var samples = new ArrayList<ClientBehaviorInput.Sample>();
        var input = new ClientBehaviorInput(gate, () -> now[0], () -> true, samples::add);
        input.bindAcceptedRequest("ep", 25);
        input.set(1, 0, false, false, true);
        input.armDefault("ep", 25, 2, 0, 0, false, true, false, 8, 400_000_000L);

        String payload = "{\"schema_version\":\"mc2p.client_action.v2\",\"episode_id\":\"ep\","
                + "\"request_sequence_id\":26,\"observation_sequence_id\":7,"
                + "\"remaining_budget_ns\":100000000,\"observation_budget_ns\":100000000,"
                + "\"valid_for_ticks\":1,"
                + "\"movement\":{\"forward\":1,\"strafe\":0,\"jump\":true,\"sneak\":false,\"sprint\":true},"
                + "\"look\":{\"yaw_delta_degrees\":0.0,\"pitch_delta_degrees\":0.0},"
                + "\"operation\":null,\"cancel_request_sequence_id\":null,"
                + "\"first_effect_window\":{\"earliest_tick\":1,\"latest_tick\":1},"
                + "\"safe_default\":{\"activation_tick\":2,"
                + "\"movement\":{\"forward\":0,\"strafe\":0,\"jump\":false,\"sneak\":true,\"sprint\":false},"
                + "\"valid_for_ticks\":8,\"wall_budget_ns\":400000000}}";
        var successor = ClientActionRequest.decode(bytes(payload), "mc2p.client_action.v2");
        var admitted = ClientTimingAdmission.admit(gate, successor, 7, 100_000_000L, now[0], 1);
        yes(admitted.denial() == null);
        yes(successor.requiredSafeDefaultBudgetNs(1) > 100_000_000L); // Executor rejects before stageWindowed.
        input.reportNotApplied(input.sampleCount() + 1L, "ep", 26);
        boolean installed = input.rejectTimingForRequest(26, false);
        yes(!installed);
        yes(ClientTimingAdmission.preStageRejectionDisposition(successor, false)
                == ClientTimingAdmission.RejectionDisposition.HOLD_INCUMBENT);
        yes(ClientTimingAdmission.preStageRejectionDisposition(successor, true)
                == ClientTimingAdmission.RejectionDisposition.HARD_RELEASE);
        yes(input.holdRejectedResponseForFallback());
        yes(!input.timingResponseReady());
        yes("duplicate_request".equals(gate.admit("ep", 26, 7, 7, 1_000_000_000L, 1, now[0])));

        for (int tick = 1; tick <= 9; tick++) {
            input.prepareForPlayerTick();
            input.tick(false, 1.0F);
        }
        yes(!input.timingResponseReady());
        input.prepareForPlayerTick(); // Tick 10 reports exhaustion before sampling neutral.
        input.tick(false, 1.0F);
        yes(input.timingResponseReady());
        yes(samples.stream().filter(sample -> sample.state().equals("default_active")).count() == 8);
        var events = input.drainTimingEvents();
        yes(events.stream().anyMatch(event -> event.status().equals("default_exhausted")
                && event.movementTickId() == 10));
        System.out.print("SAMPLES=");
        for (var sample : samples) System.out.printf("%d:%d:%s:%s;", sample.movementTickId(),
                sample.requestSequenceId(), sample.state(), sample.sneak());
        System.out.print("\nEVENTS=");
        for (var event : events) System.out.printf("%s:%d:%d;", event.status(), event.movementTickId(),
                event.requestSequenceId());
        System.out.println();

        var pendingGate = new ClientRequestGate();
        yes(pendingGate.admit("ep", 35, 7, 7, 2_000_000_000L, 1, now[0]) == null);
        var pendingInput = new ClientBehaviorInput(pendingGate, () -> now[0], () -> true);
        pendingInput.bindAcceptedRequest("ep", 35);
        yes(pendingGate.admitPending("ep", 36, 7, 7, 2_000_000_000L, 1) == null);
        pendingInput.stageWindowed("ep", 36, 1, 3, 3, now[0] + 1_000_000_000L,
                1, 1, 0, true, false, true, "pending", null);
        var pendingDuplicateAction = ClientActionRequest.decode(bytes(payload.replace(
                "\"request_sequence_id\":26", "\"request_sequence_id\":36")),
                "mc2p.client_action.v2");
        var duplicatePending = ClientTimingAdmission.admit(pendingGate, pendingDuplicateAction, 7,
                2_000_000_000L, now[0], 1);
        yes("duplicate_request".equals(duplicatePending.denial()));
        yes(!pendingInput.rejectTimingForRequest(36, false));
        yes(pendingInput.timingOwns(36));

        var parentGate = new ClientRequestGate();
        yes(parentGate.admit("ep", 45, 7, 7, 2_000_000_000L, 1, now[0]) == null);
        var parentInput = new ClientBehaviorInput(parentGate, () -> now[0], () -> true);
        parentInput.bindAcceptedRequest("ep", 45);
        parentInput.armDefault("ep", 45, 2, 0, 0, false, true, false, 8, 400_000_000L);
        String duplicateParentPayload = payload.replace("\"request_sequence_id\":26", "\"request_sequence_id\":45")
                .replace("\"first_effect_window\":{\"earliest_tick\":1,\"latest_tick\":1}",
                         "\"first_effect_window\":null");
        var duplicateParent = ClientActionRequest.decode(bytes(duplicateParentPayload),
                "mc2p.client_action.v2");
        yes("duplicate_request".equals(ClientTimingAdmission.admit(parentGate, duplicateParent, 7,
                2_000_000_000L, now[0], 1).denial()));
        yes(!parentInput.rejectTimingForRequest(45, false));
        yes(parentInput.holdRejectedResponseForFallback());
        yes(!parentInput.timingResponseReady());
        for (int tick = 1; tick <= 10; tick++) {
            parentInput.prepareForPlayerTick();
            parentInput.tick(false, 1.0F);
        }
        yes(parentInput.timingResponseReady());
        yes(parentInput.drainSamples().stream()
                .filter(sample -> sample.state().equals("default_active")).count() == 8);
        System.out.println("CLIENT_TIMING_REJECT_ISOLATION_OK");
    }
}
