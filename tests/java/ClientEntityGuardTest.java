import com.mc2p.actions.ClientEntityGuard;
import java.util.Objects;

public final class ClientEntityGuardTest {
    private static void expect(String expected, String actual) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(expected + " != " + actual);
        }
    }

    public static void main(String[] args) {
        expect(null, ClientEntityGuard.validate("entity-1", "entity-1", 2.8, 3.0));
        expect(null, ClientEntityGuard.validate("entity-1", "entity-1", 3.0, 3.0));
        expect("target_miss", ClientEntityGuard.validate("entity-1", null, 0.0, 3.0));
        expect("wrong_entity_target", ClientEntityGuard.validate("entity-1", "entity-2", 2.0, 3.0));
        expect("target_out_of_reach", ClientEntityGuard.validate("entity-1", "entity-1", 3.01, 3.0));
        for (double distance : new double[] {-1, Double.NaN, Double.POSITIVE_INFINITY}) {
            expect("target_out_of_reach", ClientEntityGuard.validate("entity-1", "entity-1", distance, 3.0));
        }
        for (double reach : new double[] {-1, Double.NaN, Double.POSITIVE_INFINITY}) {
            expect("target_out_of_reach", ClientEntityGuard.validate("entity-1", "entity-1", 2.0, reach));
        }
        System.out.println("CLIENT_ENTITY_GUARD_OK");
    }
}
