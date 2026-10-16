import com.mc2p.diagnostics.ClientFtickDiagnostics;
import com.mc2p.diagnostics.ClientTimeDiagnostics;
import java.nio.file.Files;
import java.nio.file.Path;

public final class ClientFtickDiagnosticsTest {
    public static void main(String[] args) {
        ClientTimeDiagnostics.initialize();
        ClientFtickDiagnostics.frameReceived("fabric-21001-0", 40, 7, 10);
        ClientFtickDiagnostics.inputSampled("fabric-21001-0", 7, 101, 20);
        ClientFtickDiagnostics.movementApplied("fabric-21001-0", 7, 101, 30);
        ClientFtickDiagnostics.observationSent("fabric-21001-0", 40, 7, 41, 101L, 40);
        if (Files.exists(Path.of("ftick-events", "segment-00000000.jsonl"))) {
            throw new AssertionError("F-TICK event reached disk before close");
        }
        if (args.length == 1 && args[0].equals("duplicate")) {
            ClientFtickDiagnostics.frameReceived("fabric-21001-0", 40, 7, 50);
        }
        ClientTimeDiagnostics.close();
        System.out.println("CLIENT_FTICK_DIAGNOSTICS_OK");
    }
}
