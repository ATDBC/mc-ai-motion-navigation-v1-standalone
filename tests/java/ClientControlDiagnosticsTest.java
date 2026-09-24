import com.mc2p.diagnostics.ClientControlDiagnostics;
import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;

public final class ClientControlDiagnosticsTest {
    public static void main(String[] args) {
        ClientTimeDiagnostics.initialize();
        var client = MinecraftClient.getInstance();
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client, 0);
        ClientControlDiagnostics.lookApplied(client, "probe", 4, 1_000_000_010L);
        ClientControlDiagnostics.attackDispatched(
            client, "probe", 4, 1_000_000_015L, "entity-7");
        client.player.velocity = new MinecraftClient.Velocity(.2, 0, -.1);
        ClientControlDiagnostics.inputConsumed(
            client, "probe", 4, 1_000_000_020L, "leased",
            1f, -1f, false, false, false);
        ClientTimeDiagnostics.clientTick();
        ClientControlDiagnostics.inputConsumed(
            client, "probe", 4, 1_000_000_030L, "lease_exhausted",
            0f, 0f, false, false, false);
        if (args.length == 0) {
            ClientTimeDiagnostics.close();
            ClientControlDiagnostics.lookApplied(client, "probe", 5, 1_000_000_040L);
        }
        System.out.println("CLIENT_CONTROL_DIAGNOSTICS_OK");
    }
}
