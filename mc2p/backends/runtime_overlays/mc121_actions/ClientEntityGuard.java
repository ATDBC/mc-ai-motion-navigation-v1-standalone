package com.mc2p.actions;

/** Binds an attack request to the freshly raycast entity and current vanilla reach. */
public final class ClientEntityGuard {
    private ClientEntityGuard() {}

    public static String validate(String requested, String actual, double distance, double reach) {
        if (actual == null) return "target_miss";
        if (!actual.equals(requested)) return "wrong_entity_target";
        if (!Double.isFinite(distance) || !Double.isFinite(reach) || distance < 0 || reach < 0
                || distance > reach) return "target_out_of_reach";
        return null;
    }
}
