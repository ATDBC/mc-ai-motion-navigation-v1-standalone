import com.mc2p.actions.ClientActionRequest;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public final class ClientActionTimingDecodeTest {
    private static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    private static void reject(String payload, String schema) {
        try { ClientActionRequest.decode(bytes(payload), schema); }
        catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("accepted invalid timing action");
    }

    public static void main(String[] args) {
        String base = "{\"schema_version\":\"mc2p.client_action.v2\",\"episode_id\":\"ep\"," +
                "\"request_sequence_id\":25,\"observation_sequence_id\":7," +
                "\"remaining_budget_ns\":2000000000,\"observation_budget_ns\":2000000000," +
                "\"valid_for_ticks\":1," +
                "\"movement\":{\"forward\":1,\"strafe\":0,\"jump\":false,\"sneak\":false,\"sprint\":true}," +
                "\"look\":{\"yaw_delta_degrees\":0.0,\"pitch_delta_degrees\":0.0}," +
                "\"operation\":null,\"cancel_request_sequence_id\":null," +
                "\"first_effect_window\":null," +
                "\"safe_default\":{\"activation_tick\":27," +
                "\"movement\":{\"forward\":0,\"strafe\":0,\"jump\":false,\"sneak\":true,\"sprint\":false}," +
                "\"valid_for_ticks\":8,\"wall_budget_ns\":400000000}}";
        ClientActionRequest parent = ClientActionRequest.decode(bytes(base), "mc2p.client_action.v2");
        yes(parent.safeDefault() != null && parent.safeDefault().movement().sneak());
        yes(parent.safeDefault().activationTick() == 27 && parent.safeDefault().validForTicks() == 8);
        yes(parent.requiredSafeDefaultBudgetNs(26) == 450_000_000L);
        yes(parent.timingAdmissionDenial(26) == null);

        String successor = base.replace("\"request_sequence_id\":25", "\"request_sequence_id\":26")
                .replace("\"jump\":false,\"sneak\":false", "\"jump\":true,\"sneak\":false")
                .replace("\"first_effect_window\":null", "\"first_effect_window\":{\"earliest_tick\":27,\"latest_tick\":27}")
                .replace(base.substring(base.indexOf("\"safe_default\":")), "\"safe_default\":null}");
        ClientActionRequest next = ClientActionRequest.decode(bytes(successor), "mc2p.client_action.v2");
        yes(next.firstEffectWindow().earliestTick() == 27 && next.jump());
        yes(next.windowedActivationDenial() == null);
        ClientActionRequest windowedOperation = ClientActionRequest.decode(bytes(successor.replace(
                "\"operation\":null", "\"operation\":{\"kind\":\"open_inventory\"}")),
                "mc2p.client_action.v2");
        yes("windowed_operation_unsupported".equals(windowedOperation.windowedActivationDenial()));

        try {
            for (String row : Files.readAllLines(Path.of("tests/fixtures/d122_default_relation_vectors.tsv")).subList(1, 9)) {
                String[] value = row.split("\\t");
                String candidate = base
                        .replace("\"valid_for_ticks\":1", "\"valid_for_ticks\":" + value[2])
                        .replace("\"activation_tick\":27", "\"activation_tick\":" + value[5]);
                if (value[0].equals("windowed")) {
                    candidate = candidate.replace("\"first_effect_window\":null",
                            "\"first_effect_window\":{\"earliest_tick\":" + value[3]
                                    + ",\"latest_tick\":" + value[4] + "}");
                }
                ClientActionRequest vector = ClientActionRequest.decode(bytes(candidate), "mc2p.client_action.v2");
                String denial = vector.timingAdmissionDenial(Long.parseLong(value[1]));
                yes((denial == null) == Boolean.parseBoolean(value[6]));
            }
        } catch (java.io.IOException error) { throw new AssertionError(error); }
        ClientActionRequest huge = ClientActionRequest.decode(bytes(base.replace(
                "\"activation_tick\":27", "\"activation_tick\":9223372036854775807")),
                "mc2p.client_action.v2");
        yes(huge.requiredSafeDefaultBudgetNs(0) == Long.MAX_VALUE);

        reject(base, "mc2p.client_action.v1");
        reject(base.replace("\"activation_tick\":27", "\"activation_tick\":-1"), "mc2p.client_action.v2");
        reject(base.replace("\"valid_for_ticks\":8", "\"valid_for_ticks\":21"), "mc2p.client_action.v2");
        reject(base.replace("\"wall_budget_ns\":400000000", "\"wall_budget_ns\":399999999"), "mc2p.client_action.v2");
        reject(base.replace("\"safe_default\":{", "\"safe_default\":{\"safe_default\":null,"), "mc2p.client_action.v2");
        reject(successor.replace("\"earliest_tick\":27,\"latest_tick\":27", "\"earliest_tick\":28,\"latest_tick\":27"), "mc2p.client_action.v2");
        reject(successor.replace("\"latest_tick\":27", "\"latest_tick\":9223372036854775808"), "mc2p.client_action.v2");
        System.out.println("CLIENT_ACTION_TIMING_DECODE_OK");
    }
}
