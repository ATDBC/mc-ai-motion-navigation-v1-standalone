import com.mc2p.deployment.DeploymentSession;
import java.nio.charset.StandardCharsets;

public class DeploymentSessionTest {
    static final String TOKEN = "0123456789abcdef".repeat(4);
    static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    static void reject(byte[] payload) {
        try { DeploymentSession.decode(payload, TOKEN); throw new AssertionError("accepted invalid session"); }
        catch (IllegalArgumentException expected) {
            if (expected.toString().contains(TOKEN)) throw new AssertionError("credential leaked");
        }
    }
    public static void main(String[] args) {
        String legacy = "{\"schema_version\":\"mc2p.deployment_session.v1\",\"episode_id\":\"deployment:1\",\"token\":\"" + TOKEN + "\"}";
        DeploymentSession v1 = DeploymentSession.decode(bytes(legacy), TOKEN);
        if (!v1.episode().equals("deployment:1") || !v1.observationSchemaVersion().equals("mc2p.client_observation.v2"))
            throw new AssertionError("legacy session changed");
        String good = "{\"schema_version\":\"mc2p.deployment_session.v2\",\"episode_id\":\"deployment:1\",\"token\":\"" + TOKEN
                + "\",\"observation_schema_version\":\"mc2p.client_observation.v3\"}";
        DeploymentSession v2 = DeploymentSession.decode(bytes(good), TOKEN);
        if (!v2.episode().equals("deployment:1") || !v2.observationSchemaVersion().equals("mc2p.client_observation.v3"))
            throw new AssertionError("V3 session negotiation lost");
        for (String bad : new String[]{"{}", "[]", good + " {}", good.replace("session.v2", "session.v0"),
                good.replace("deployment:1", "../bad space"), good.replace("deployment:1", ""),
                good.replace(TOKEN, "f".repeat(64)), good.replace("\"deployment:1\"", "42"),
                good.replace("\"deployment:1\"", "null"), good.replace("\"deployment:1\"", "{}"),
                good.replace("\"episode_id\":", "\"episode_id\":\"other\",\"episode_id\":"),
                good.replace("}", ",\"command\":\"stop\"}"), good.replace("\"schema_version\"", "schema_version"),
                good.replace("mc2p.client_observation.v3", "mc2p.client_observation.v2"),
                legacy.replace("}", ",\"observation_schema_version\":\"mc2p.client_observation.v3\"}")}) reject(bytes(bad));
        reject(new byte[0]); reject(new byte[16385]); reject(new byte[]{(byte) 0xff});
        try { DeploymentSession.decode(bytes(good), "short"); throw new AssertionError(); }
        catch (IllegalArgumentException expected) {}
        System.out.println("DEPLOYMENT_SESSION_OK");
    }
}
