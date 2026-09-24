import com.mc2p.actions.ClientActionRequest;
import java.nio.charset.StandardCharsets;

public final class ClientActionDecodeTest {
    public static void main(String[] args) {
        try {
            ClientActionRequest action = ClientActionRequest.decode(args[0].getBytes(StandardCharsets.UTF_8));
            System.out.println("accepted:" + action.operationKind());
        } catch (IllegalArgumentException error) {
            System.out.println("rejected");
        }
    }
}
