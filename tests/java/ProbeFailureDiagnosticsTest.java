import com.mc2p.deployment.ProbeFailureDiagnostics;
import java.io.IOException;

public class ProbeFailureDiagnosticsTest {
    private static final String SECRET = "token=secret-payload@127.0.0.1:8123";

    private static <T extends Throwable> T frames(T error, StackTraceElement... frames) {
        error.setStackTrace(frames);
        return error;
    }

    private static StackTraceElement frame(String owner, String file, int line) {
        return new StackTraceElement(owner, "run", file, line);
    }

    private static void expect(Throwable error, String expected) {
        String actual = ProbeFailureDiagnostics.format(error);
        if (!expected.equals(actual)) throw new AssertionError(actual + " != " + expected);
        if (actual.contains(SECRET) || actual.contains("suppressed-secret"))
            throw new AssertionError("diagnostic leaked a message");
    }

    public static void main(String[] args) {
        var project = frame("com.mc2p.deployment.ProbeTransport", "ProbeTransport.java", 119);
        var overlay = frame("com.kyhsgeekcode.minecraftenv.Client", "Client.java", 23);
        var jdk = frame("java.io.InputStream", "InputStream.java", 42);
        var direct = frames(new IllegalArgumentException(SECRET), project);
        direct.addSuppressed(new RuntimeException("suppressed-secret"));
        expect(direct, "IllegalArgumentException:cause=IllegalArgumentException:source=ProbeTransport.java:119");

        var wrapped = ProbeFailureDiagnostics.transportFailure(direct);
        if (wrapped.getCause() != direct) throw new AssertionError("transport cause was lost");
        expect(wrapped, "IllegalStateException:cause=IllegalArgumentException:source=ProbeTransport.java:119");
        var io = frames(new IOException(SECRET), jdk, project);
        expect(new IllegalStateException(SECRET, io),
                "IllegalStateException:cause=IOException:source=ProbeTransport.java:119");
        var overlayCause = frames(new IllegalArgumentException(SECRET), overlay);
        expect(new RuntimeException(SECRET, new IllegalStateException(SECRET, overlayCause)),
                "RuntimeException:cause=IllegalArgumentException:source=Client.java:23");
        // The deepest cause may have no project frame; use the nearest useful wrapper.
        var jdkOnly = frames(new IOException(SECRET), jdk);
        var usefulWrapper = frames(new IllegalStateException(SECRET, jdkOnly), overlay);
        expect(usefulWrapper, "IllegalStateException:cause=IllegalStateException:source=Client.java:23");

        expect(frames(new IOException(SECRET), jdk), "IOException:cause=IOException:source=unknown");
        for (String unsafe : new String[]{"C:\\secret\\Probe.java", "/secret/Probe.java",
                "../Probe.java", "bad\nname.java", "token=abc.java", ""}) {
            expect(frames(new IOException(SECRET), frame("com.mc2p.Client", unsafe, 12)),
                    "IOException:cause=IOException:source=unknown");
        }
        expect(frames(new IOException(SECRET), frame("com.mc2p.evil.Client", "Probe.java", -1),
                frame("com.mc2pSpoof.Client", "Probe.java", 12)),
                "IOException:cause=IOException:source=unknown");
        // Standard Throwable allows an indirect cause cycle.
        var first = frames(new RuntimeException(SECRET), project);
        var second = frames(new IllegalStateException(SECRET), overlay);
        first.initCause(second);
        second.initCause(first);
        expect(first, "RuntimeException:cause=IllegalStateException:source=Client.java:23");
        Throwable chain = frames(new IOException(SECRET), jdk);
        for (int i = 0; i < 100; i++) chain = frames(new RuntimeException(SECRET, chain), project);
        expect(chain, "RuntimeException:cause=RuntimeException:source=ProbeTransport.java:119");
        System.out.println("PROBE_FAILURE_DIAGNOSTICS_OK");
    }
}
