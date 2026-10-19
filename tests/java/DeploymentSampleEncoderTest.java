import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mc2p.deployment.DeploymentSampleEncoder;
import java.nio.charset.StandardCharsets;

public final class DeploymentSampleEncoderTest {
    public static void main(String[] args) {
        byte[] observation = "{\"schema_version\":\"mc2p.client_observation.v3\",\"value\":7}"
                .getBytes(StandardCharsets.UTF_8);
        var receipt = new JsonObject();
        receipt.addProperty("generation", 3);
        var diagnostics = new JsonObject();
        diagnostics.addProperty("kind", "pipeline");

        byte[] encoded = DeploymentSampleEncoder.encode(
                "episode-\"quoted", observation, receipt, diagnostics);
        var root = JsonParser.parseString(new String(encoded, StandardCharsets.UTF_8))
                .getAsJsonObject();
        require(root.get("episode_id").getAsString().equals("episode-\"quoted"),
                "episode was not JSON escaped");
        require(root.getAsJsonObject("observation").get("value").getAsInt() == 7,
                "observation changed while embedding");
        require(root.getAsJsonObject("receipt").get("generation").getAsInt() == 3,
                "receipt missing");
        require(root.getAsJsonObject("diagnostics").get("kind").getAsString().equals("pipeline"),
                "diagnostics missing");
        String wire = new String(encoded, StandardCharsets.UTF_8);
        require(wire.contains(new String(observation, StandardCharsets.UTF_8)),
                "raw observation bytes were not preserved");

        reject("[]".getBytes(StandardCharsets.UTF_8));
        reject("{} ".getBytes(StandardCharsets.UTF_8));
        System.out.println("DEPLOYMENT_SAMPLE_ENCODER_OK");
    }

    private static void reject(byte[] observation) {
        try {
            DeploymentSampleEncoder.encode("episode", observation, new JsonObject(), new JsonObject());
            throw new AssertionError("invalid observation framing was accepted");
        } catch (IllegalArgumentException expected) {
            // Expected.
        }
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
