package com.mc2p.actions;

/** Small typed rules shared by request admission and attack dispatch. */
public final class ClientOperationCompatibility {
    private ClientOperationCompatibility() {}

    public static boolean allowsControls(String operationKind) {
        if (operationKind == null) throw new IllegalArgumentException("operation kind");
        return operationKind.equals("neutral") || operationKind.equals("attack_entity");
    }

    public static boolean cooldownSatisfied(float actual, float required) {
        if (!Float.isFinite(actual) || !Float.isFinite(required)
                || actual < 0.0f || actual > 1.0f
                || required < 0.0f || required > 1.0f) {
            throw new IllegalArgumentException("attack cooldown progress");
        }
        return actual >= required;
    }
}
