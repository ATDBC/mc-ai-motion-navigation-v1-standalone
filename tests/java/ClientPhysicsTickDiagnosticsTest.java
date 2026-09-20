import com.mc2p.diagnostics.ClientPhysicsTickDiagnostics;
import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;

public final class ClientPhysicsTickDiagnosticsTest {
    public static void main(String[] args) {
        ClientTimeDiagnostics.initialize();
        var client = MinecraftClient.getInstance();
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client, 0);

        client.player.x = 1.5; client.player.y = 64; client.player.z = 2.5;
        client.player.yaw = 30; client.player.pitch = -5;
        client.player.onGround = true;
        client.player.horizontalCollision = false;
        client.player.verticalCollision = false;
        client.player.velocity = new MinecraftClient.Velocity(.1, 0, 0);
        ClientPhysicsTickDiagnostics.beforeMovement(client);
        ClientPhysicsTickDiagnostics.afterInputSample(
            client, "physics", 7, 2_000_000_010L, "leased",
            1f, -.3f, true, false, true);

        client.player.x = 1.62; client.player.y = 64.42; client.player.z = 2.48;
        client.player.onGround = false;
        client.player.velocity = new MinecraftClient.Velocity(.12, .42, -.02);
        ClientPhysicsTickDiagnostics.afterMovement(client);
        ClientTimeDiagnostics.close();
        System.out.println("CLIENT_PHYSICS_TICK_DIAGNOSTICS_OK");
    }
}
