import com.mc2p.actions.ClientActionRequest;
import com.mc2p.actions.ClientRequestGate;
import com.mc2p.actions.ClientTimingAdmission;
import java.nio.charset.StandardCharsets;

public final class ClientTimingAdmissionTest {
    private static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }

    private static String payload(long sequence, String operation, String window, long activation) {
        return "{\"schema_version\":\"mc2p.client_action.v2\",\"episode_id\":\"ep\","
                + "\"request_sequence_id\":" + sequence + ",\"observation_sequence_id\":7,"
                + "\"remaining_budget_ns\":2000000000,\"observation_budget_ns\":2000000000,"
                + "\"valid_for_ticks\":1,"
                + "\"movement\":{\"forward\":1,\"strafe\":0,\"jump\":false,\"sneak\":false,\"sprint\":true},"
                + "\"look\":{\"yaw_delta_degrees\":0.0,\"pitch_delta_degrees\":0.0},"
                + "\"operation\":" + operation + ",\"cancel_request_sequence_id\":null,"
                + "\"first_effect_window\":" + window + ","
                + "\"safe_default\":{\"activation_tick\":" + activation + ","
                + "\"movement\":{\"forward\":0,\"strafe\":0,\"jump\":false,\"sneak\":true,\"sprint\":false},"
                + "\"valid_for_ticks\":8,\"wall_budget_ns\":400000000}}";
    }

    public static void main(String[] args) {
        long now = 1_000_000_000L;
        var ordinaryGate = new ClientRequestGate();
        var badParent = ClientActionRequest.decode(bytes(payload(25, "null", "null", 28)),
                "mc2p.client_action.v2");
        var ordinary = ClientTimingAdmission.admit(ordinaryGate, badParent, 7, 2_000_000_000L, now, 26);
        yes("safe_default_activation_mismatch".equals(ordinary.denial()));
        yes(!ordinaryGate.leaseActive(now));
        yes("duplicate_request".equals(ordinaryGate.admit("ep", 25, 7, 7,
                2_000_000_000L, 1, now)));

        var windowGate = new ClientRequestGate();
        var badWindow = ClientActionRequest.decode(bytes(payload(26, "{\"kind\":\"open_inventory\"}",
                "{\"earliest_tick\":27,\"latest_tick\":27}", 28)), "mc2p.client_action.v2");
        var windowed = ClientTimingAdmission.admit(windowGate, badWindow, 7, 2_000_000_000L, now, 26);
        yes("windowed_operation_unsupported".equals(windowed.denial()));
        yes(!windowGate.leaseActive(now));
        yes("duplicate_request".equals(windowGate.admit("ep", 26, 7, 7,
                2_000_000_000L, 1, now)));
        yes(ClientTimingAdmission.preStageRejectionDisposition(badWindow, false)
                == ClientTimingAdmission.RejectionDisposition.HOLD_INCUMBENT);
        yes(ClientTimingAdmission.preStageRejectionDisposition(badWindow, true)
                == ClientTimingAdmission.RejectionDisposition.HARD_RELEASE);
        System.out.println("CLIENT_TIMING_ADMISSION_OK");
    }
}
