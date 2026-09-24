import com.mc2p.democontrol.DemoControlConnection;
import com.mc2p.democontrol.DemoCommands.DemoCommand;

public final class DemoControlConnectionTest {
    public static void main(String[] args) throws Exception {
        try (var connection = DemoControlConnection.fromEnvironment(System.getenv())) {
            connection.start();
            long deadline = System.nanoTime() + 8_000_000_000L;
            boolean sent = false;
            while (System.nanoTime() < deadline) {
                String message;
                while ((message = connection.pollFeedback()) != null) {
                    System.out.println(message); System.out.flush();
                }
                if (connection.isReady() && !sent) {
                    if (!connection.submit(new DemoCommand("follow_status", null, null))) throw new AssertionError();
                    sent = true;
                }
                if (connection.failure() != null) {
                    System.out.println("FAILURE:" + connection.failure()); return;
                }
                Thread.sleep(10);
            }
            if (!sent || !connection.isReady()) throw new AssertionError("not connected");
            System.out.println("CONNECTION_OK");
        }
    }
}
