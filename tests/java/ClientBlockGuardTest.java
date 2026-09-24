import com.mc2p.actions.ClientBlockGuard;
import com.mc2p.actions.ClientBlockGuard.Target;
import java.util.Objects;

public final class ClientBlockGuardTest {
    private static void check(String expected, String actual) {
        if (!Objects.equals(expected, actual)) throw new AssertionError(expected + " != " + actual);
    }
    public static void main(String[] args) {
        Target requested = new Target(1, -60, 3, "up");
        check(null, ClientBlockGuard.validate(requested, requested, 4.49, 4.5));
        check(null, ClientBlockGuard.validate(requested, requested, 4.5, 4.5));
        check("no_block_target", ClientBlockGuard.validate(requested, null, 0, 4.5));
        check("target_mismatch", ClientBlockGuard.validate(requested, new Target(2, -60, 3, "up"), 2, 4.5));
        check("target_mismatch", ClientBlockGuard.validate(requested, new Target(1, -60, 3, "north"), 2, 4.5));
        for (double distance : new double[] {-1, Double.NaN, Double.POSITIVE_INFINITY, 4.5001}) {
            check("out_of_reach", ClientBlockGuard.validate(requested, requested, distance, 4.5));
        }
        for (double reach : new double[] {-1, Double.NaN, Double.POSITIVE_INFINITY}) {
            check("out_of_reach", ClientBlockGuard.validate(requested, requested, 2, reach));
        }
        System.out.println("CLIENT_BLOCK_GUARD_OK");
    }
}
