package com.mc2p.observation;

/** Opaque identity, independent of syncId reuse, episodes and handler revision changes. */
public final class ClientGuiSession {
    private Object currentScreen, currentHandler;
    private final String namespace = java.util.UUID.randomUUID().toString();
    private long nextSession;
    private String session;

    public String observe(Object screen, Object handler) {
        if (screen == null || handler == null) {
            currentScreen = currentHandler = null;
            session = null;
        } else if (screen != currentScreen || handler != currentHandler) {
            currentScreen = screen;
            currentHandler = handler;
            nextSession = Math.incrementExact(nextSession);
            session = "gui-" + namespace + "-" + nextSession;
        }
        return session;
    }

    public void reset() {
        currentScreen = currentHandler = null;
        session = null;
    }
}
