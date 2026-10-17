import java.nio.charset.StandardCharsets;
import java.util.Base64;
public final class LaunchArgumentEcho {
    public static void main(String[] args) {
        for (String value : args)
            System.out.println(Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8)));
    }
}
