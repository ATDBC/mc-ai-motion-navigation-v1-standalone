package com.kyhsgeekcode.minecraftenv;

/** Training clock only: settle loops pump the client without extra local-player simulation. */
public final class LockstepClientSimulationGate {
    private static final Thread OWNER = Thread.currentThread();
    private static boolean playerTickAllowed = true;
    private static void requireThread() {
        if (Thread.currentThread() != OWNER) throw new IllegalStateException("client simulation gate off client thread");
    }
    public static void beginCycle(boolean initializing) {
        requireThread();
        playerTickAllowed = initializing;
    }
    public static void admitAction() {
        requireThread();
        playerTickAllowed = true;
    }
    public static boolean allowPlayerTick() {
        requireThread();
        return playerTickAllowed;
    }
}
