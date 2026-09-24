import com.mc2p.actions.ClientOperationCompatibility;

public final class ClientOperationCompatibilityTest {
    private static void require(boolean value, String message) {
        if (!value) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        require(ClientOperationCompatibility.allowsControls("neutral"), "neutral controls");
        require(ClientOperationCompatibility.allowsControls("attack_entity"), "attack controls");
        for (String kind : new String[]{"open_inventory", "click_slot", "interact_block", "mine_block"}) {
            require(!ClientOperationCompatibility.allowsControls(kind), "unsafe combination " + kind);
        }
        require(ClientOperationCompatibility.cooldownSatisfied(0.65f, 0.65f), "exact cooldown");
        require(ClientOperationCompatibility.cooldownSatisfied(1.0f, 0.65f), "higher cooldown");
        require(!ClientOperationCompatibility.cooldownSatisfied(0.64f, 0.65f), "low cooldown");
        for (float invalid : new float[]{-0.1f, 1.1f, Float.NaN, Float.POSITIVE_INFINITY}) {
            try {
                ClientOperationCompatibility.cooldownSatisfied(1.0f, invalid);
                throw new AssertionError("invalid threshold accepted");
            } catch (IllegalArgumentException expected) {
                // Expected.
            }
        }
        System.out.println("CLIENT_OPERATION_COMPATIBILITY_OK");
    }
}
