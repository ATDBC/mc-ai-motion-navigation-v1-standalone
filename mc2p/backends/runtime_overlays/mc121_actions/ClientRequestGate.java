package com.mc2p.actions;

/** Client-thread-only request identity and bounded continuous-control ownership. */
public final class ClientRequestGate {
    public static long remainingBudget(long senderBudget, long observationBudget, long elapsedSinceObservation) {
        if (senderBudget <= 0 || elapsedSinceObservation < 0 || elapsedSinceObservation >= observationBudget)
            return 0;
        return Math.min(senderBudget, observationBudget - elapsedSinceObservation);
    }

    private final Thread ownerThread = Thread.currentThread();
    private String episode;
    private long lastSequence = -1;
    private long leaseOwner = -1;
    private long leaseStart;
    private long leaseBudget;
    private int ticksRemaining;

    public void requireOwnerThread() {
        if (Thread.currentThread() != ownerThread)
            throw new IllegalStateException("client request gate used outside owner thread");
    }

    public String admit(String requestedEpisode, long sequence, long observation,
                        long latestObservation, long budget, int ticks, long now) {
        String rejection = validate(requestedEpisode, sequence, observation, latestObservation, budget, ticks);
        if (rejection != null) return rejection;
        leaseOwner = sequence;
        leaseStart = now;
        leaseBudget = budget;
        ticksRemaining = ticks;
        return null;
    }

    public String validate(String requestedEpisode, long sequence, long observation,
                           long latestObservation, long budget, int ticks) {
        requireOwnerThread();
        if (requestedEpisode == null || !requestedEpisode.matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
                || sequence < 0 || observation < 0 || latestObservation < 0)
            return "invalid_request";
        if (episode != null && !episode.equals(requestedEpisode)) return "episode_mismatch";
        if (sequence <= lastSequence) return "duplicate_request";
        // Even a rejected request cannot later be retried as a different side effect.
        episode = requestedEpisode;
        lastSequence = sequence;
        if (observation != latestObservation) return "stale_observation";
        if (budget <= 0) return "deadline_exceeded";
        if (budget > 30_000_000_000L || ticks < 1 || ticks > 20) return "invalid_request";
        return null;
    }

    public boolean leaseActive(long now) {
        requireOwnerThread();
        return ticksRemaining > 0 && now - leaseStart < leaseBudget;
    }

    public boolean wallClockExpired(long now) {
        requireOwnerThread();
        // Logical exhaustion is intentionally separate: vanilla may still consume the last input edge.
        return leaseOwner >= 0 && now - leaseStart >= leaseBudget;
    }

    public void endControlTick() {
        requireOwnerThread();
        ticksRemaining = Math.max(0, ticksRemaining - 1);
    }

    public boolean cancel(long requestSequence) {
        requireOwnerThread();
        if (leaseOwner != requestSequence) return false;
        ticksRemaining = 0;
        return true;
    }

    public void reset() {
        requireOwnerThread();
        episode = null;
        lastSequence = -1;
        leaseOwner = -1;
        ticksRemaining = 0;
    }
}
