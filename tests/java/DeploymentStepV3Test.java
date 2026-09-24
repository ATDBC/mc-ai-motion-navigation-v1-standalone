import com.mc2p.deployment.DeploymentStepV3;
import java.nio.charset.StandardCharsets;

public final class DeploymentStepV3Test {
    private static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    private static void reject(byte[] payload) {
        try { DeploymentStepV3.decode(payload); }
        catch (IllegalArgumentException expected) {
            if (expected.getMessage() != null && expected.getMessage().contains("secret"))
                throw new AssertionError("input leaked through decoder error");
            return;
        }
        throw new AssertionError("invalid V3 step accepted");
    }
    public static void main(String[] args) {
        String action = "{\"schema_version\":\"mc2p.client_action.v1\",\"episode_id\":\"deployment:1\","
                + "\"request_sequence_id\":1,\"observation_sequence_id\":0,\"remaining_budget_ns\":1000000,"
                + "\"observation_budget_ns\":2000000,\"valid_for_ticks\":1,"
                + "\"movement\":{\"forward\":0,\"strafe\":0,\"jump\":false,\"sneak\":false,\"sprint\":false},"
                + "\"look\":{\"yaw_delta_degrees\":0.0,\"pitch_delta_degrees\":0.0},"
                + "\"operation\":null,\"cancel_request_sequence_id\":null}";
        String request = "{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"interaction_v1\","
                + "\"air_positions\":[],\"entity_track_id\":\"entity-world-7\"}";
        String good = "{\"schema_version\":\"mc2p.client_step.v3\",\"action\":" + action
                + ",\"observation_request\":" + request + "}";
        DeploymentStepV3 decoded = DeploymentStepV3.decode(bytes(good));
        if (!decoded.action().episode().equals("deployment:1") || decoded.action().sequence() != 1)
            throw new AssertionError("action decode lost");
        if (!decoded.observationRequest().fieldProfile().equals("interaction_v1"))
            throw new AssertionError("profile decode lost");
        if (!decoded.observationRequest().entityTrackId().equals("entity-world-7"))
            throw new AssertionError("entity query decode lost");
        if (decoded.actionPayload().length == 0) throw new AssertionError("executor payload lost");

        for (String bad : new String[]{"{}", "[]", good + " {}", good.replace("client_step.v3", "client_step.v2"),
                good.replace("\"schema_version\":\"mc2p.client_step.v3\"", "\"schema_version\":\"mc2p.client_step.v3\",\"schema_version\":\"mc2p.client_step.v3\""),
                good.replace("\"action\":", "\"extra\":0,\"action\":"),
                good.replace("\"action\":" + action, "\"action\":null"),
                good.replace("\"observation_request\":" + request, "\"observation_request\":[]"),
                good.replace("interaction_v1", "bad_profile"),
                good.replace("\"field_profile\":", "\"field_profile\":\"navigation_v1\",\"field_profile\":"),
                good.replace("entity-world-7", ""),
                good.replace("\"entity_track_id\":", "\"entity_track_id\":\"entity-world-8\",\"entity_track_id\":"),
                good.replace("\"forward\":0", "\"forward\":NaN"),
                good.replace("\"episode_id\":\"deployment:1\"", "\"episode_id\":\"secret invalid\"")}) reject(bytes(bad));
        reject(bytes(good.replace("entity-world-7", "x".repeat(129))));
        DeploymentStepV3 none = DeploymentStepV3.decode(bytes(good.replace("\"entity-world-7\"", "null")));
        if (none.observationRequest().entityTrackId() != null) throw new AssertionError("null query changed");
        reject(new byte[0]); reject(new byte[16385]); reject(new byte[]{(byte)0xff});
        System.out.println("DEPLOYMENT_STEP_V3_OK");
    }
}
