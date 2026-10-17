import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;

public class ClientTimeDiagnosticsLifecycleTest {
    public static void main(String[] args) {
        ClientTimeDiagnostics.initialize();
        var client = MinecraftClient.getInstance();
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client, 0);
        ClientTimeDiagnostics.clientTick();
        client.world.time++;
        ClientTimeDiagnostics.worldTick(client.world, 100);
        ClientTimeDiagnostics.observation(client, 1);
        var original = new IllegalStateException("original cleanup failure");
        try {
            ClientTimeDiagnostics.closeAfter(() -> { throw original; });
            throw new AssertionError("cleanup failure was swallowed");
        } catch (IllegalStateException error) {
            if (error != original) throw new AssertionError("primary exception replaced");
        }
        ClientTimeDiagnostics.close();
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client, 2);
        System.out.println("CLIENT_TIME_LIFECYCLE_OK");
    }
}
