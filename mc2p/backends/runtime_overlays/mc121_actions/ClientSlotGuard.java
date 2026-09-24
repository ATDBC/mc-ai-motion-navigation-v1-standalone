package com.mc2p.actions;

/** Validate a slot reference against the current client screen, never against a cached handler. */
public final class ClientSlotGuard {
    private ClientSlotGuard() {}

    public static String validate(String session, int sync, int revision, int slot,
            String currentSession, int currentSync, int currentRevision, int size, boolean enabled) {
        if (currentSession == null || !currentSession.equals(session)) return "stale_gui_session";
        if (sync != currentSync) return "stale_handler";
        if (revision != currentRevision) return "stale_revision";
        if (slot < 0 || slot >= size) return "invalid_slot";
        if (!enabled) return "disabled_slot";
        return null;
    }
}
