import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;

public class ClientMovementDiagnosticsTest {
    public static void main(String[] args) throws Exception {
        ClientTimeDiagnostics.initialize();
        var client = MinecraftClient.getInstance();
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client,0);
        client.player.sprinting=true; client.player.sneaking=true; client.player.onGround=false;
        client.player.pose = MinecraftClient.Pose.CROUCHING;
        client.player.velocity = new MinecraftClient.Velocity(.2,.42,.1);
        ClientTimeDiagnostics.clientTick();
        ClientTimeDiagnostics.observation(client,1);
        client.player=null;
        ClientTimeDiagnostics.observation(client,2);
        if (args.length>0) {
            java.nio.file.Files.createFile(java.nio.file.Path.of("time-events/complete.pending"));
            try { ClientTimeDiagnostics.close(); throw new AssertionError("expected time seal failure"); }
            catch (java.io.UncheckedIOException expected) {}
        } else ClientTimeDiagnostics.close();
        ClientTimeDiagnostics.close();
        ClientTimeDiagnostics.observation(client,3);
        System.out.println("CLIENT_MOVEMENT_DIAGNOSTICS_OK");
    }
}
