package com.mc2p.actions;

/** Binds a semantic target to the freshly computed vanilla crosshair, never a cached world lookup. */
public final class ClientBlockGuard {
    private ClientBlockGuard() {}
    public record Target(int x, int y, int z, String face) {}

    public static String validate(Target requested, Target actual, double distance, double reach) {
        if (actual == null) return "no_block_target";
        if (!actual.equals(requested)) return "target_mismatch";
        if (!Double.isFinite(distance) || !Double.isFinite(reach) || distance < 0 || reach < 0
                || distance > reach) return "out_of_reach";
        return null;
    }
}
