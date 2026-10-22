package com.mc2p.actions;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.function.BooleanSupplier;
import java.util.function.LongSupplier;

/** One pending first-effect request and one proved movement-only default. Owner thread only. */
public final class ClientTimingSchedule {
    public record Movement(int forward, int strafe, boolean jump, boolean sneak, boolean sprint) {
        public Movement {
            if (forward < -1 || forward > 1 || strafe < -1 || strafe > 1)
                throw new IllegalArgumentException("invalid movement");
        }
    }
    public record Event(String status, long movementTickId, String episodeId, long requestSequenceId) {}
    public record DefaultPlan(long activationTick, Movement movement, int ticks, long wallBudgetNs) {
        public DefaultPlan {
            validateDefault(activationTick, movement, ticks, wallBudgetNs);
        }
    }
    public enum Kind { CURRENT, PENDING, DEFAULT, NEUTRAL }
    public record Selection(Kind kind, Movement movement, String episodeId, long requestSequenceId,
                            int validForTicks, long deadlineNs, Object activationPayload) {}
    private record Pending(String episode, long sequence, long earliest, long latest,
                           long deadlineNs, int ticks, Movement movement, Object activationPayload,
                           DefaultPlan nextDefault) {}
    private record Default(String episode, long sequence, long activationTick,
                           Movement movement, int ticks, long wallBudgetNs) {}

    private final LongSupplier clock;
    private final BooleanSupplier allowed;
    private final ArrayList<Event> events = new ArrayList<>();
    private Pending pending;
    private Pending appliedPending;
    private Default fallback;
    private int defaultRemaining;
    private boolean defaultActive;
    private boolean holdResponse;
    private boolean awaitingPendingSample;
    private boolean pendingPrepared;
    private boolean fallbackArmedReported;
    private long preparedTick = -1;
    private Selection prepared;

    public ClientTimingSchedule(LongSupplier clock, BooleanSupplier allowed) {
        this.clock = Objects.requireNonNull(clock);
        this.allowed = Objects.requireNonNull(allowed);
    }

    public void armDefault(String episode, long sequence, long activationTick, Movement movement,
                           int ticks, long wallBudgetNs) {
        requireIdentity(episode, sequence);
        validateDefault(activationTick, movement, ticks, wallBudgetNs);
        fallback = new Default(episode, sequence, activationTick, movement, ticks, wallBudgetNs);
        defaultRemaining = ticks;
        defaultActive = false;
        fallbackArmedReported = false;
        preparedTick = -1;
    }

    public void parentSampled(long movementTick, String episode, long sequence) {
        if (fallback != null && fallback.episode.equals(episode) && fallback.sequence == sequence
                && movementTick < fallback.activationTick && !fallbackArmedReported) {
            fallbackArmedReported = true;
            event("fallback_armed", movementTick, fallback.episode, fallback.sequence);
        }
    }

    public void admitPending(String episode, long sequence, long admittedAtTick, long earliest, long latest,
                             long deadlineNs, int ticks, Movement movement, Object activationPayload,
                             DefaultPlan nextDefault) {
        requireIdentity(episode, sequence);
        if (earliest < 0 || latest < earliest || deadlineNs <= clock.getAsLong()
                || ticks < 1 || ticks > 20 || movement == null)
            throw new IllegalArgumentException("invalid pending request");
        pending = new Pending(episode, sequence, earliest, latest, deadlineNs, ticks, movement,
                activationPayload, nextDefault);
        preparedTick = -1;
        if (admittedAtTick < 0) throw new IllegalArgumentException("invalid pending admission tick");
        event("window_pending", admittedAtTick, episode, sequence);
        holdResponse = true;
    }

    public Selection prepare(long movementTick, boolean currentLeaseActive) {
        if (movementTick < 0) throw new IllegalArgumentException("invalid movement tick");
        if (preparedTick == movementTick && prepared != null) return prepared;
        preparedTick = movementTick;
        if (!allowed.getAsBoolean()) {
            revokePermission(movementTick);
            return prepared = neutral();
        }
        long now = clock.getAsLong();
        if (fallback != null && defaultActive && defaultRemaining == 0) {
            event("default_exhausted", movementTick, fallback.episode, fallback.sequence);
            fallback = null;
            defaultActive = false;
            holdResponse = false;
        }
        if (pending != null) {
            if (movementTick > pending.latest || now >= pending.deadlineNs) {
                event("window_missed", movementTick, pending.episode, pending.sequence);
                pending = null;
                if (fallback == null) holdResponse = false;
            } else if (movementTick >= pending.earliest) {
                var value = pending;
                pendingPrepared = true;
                return prepared = new Selection(Kind.PENDING, value.movement, value.episode,
                        value.sequence, value.ticks, value.deadlineNs, value.activationPayload);
            }
        }
        if (fallback != null && movementTick >= fallback.activationTick && !currentLeaseActive) {
            if (!defaultActive) {
                defaultActive = true;
                event("default_active", movementTick, fallback.episode, fallback.sequence);
            }
            return prepared = new Selection(Kind.DEFAULT, fallback.movement, fallback.episode,
                    fallback.sequence, defaultRemaining, Long.MAX_VALUE, null);
        }
        return prepared = new Selection(Kind.CURRENT, null, null, -1, 0, 0, null);
    }

    public void sampled(long movementTick) {
        if (preparedTick != movementTick || prepared == null)
            throw new IllegalStateException("timed input sampled without preparation");
        if (awaitingPendingSample && appliedPending != null) {
            event("window_applied", movementTick, appliedPending.episode, appliedPending.sequence);
            awaitingPendingSample = false;
            appliedPending = null;
            holdResponse = false;
        } else if (prepared.kind == Kind.DEFAULT) {
            defaultRemaining--;
        }
        prepared = null;
        preparedTick = -1;
    }

    public void confirmPendingActivation() {
        if (!pendingPrepared || pending == null) throw new IllegalStateException("pending was not prepared");
        appliedPending = pending;
        DefaultPlan next = pending.nextDefault;
        pending = null;
        fallback = next == null ? null : new Default(appliedPending.episode, appliedPending.sequence,
                next.activationTick, next.movement, next.ticks, next.wallBudgetNs);
        defaultActive = false;
        defaultRemaining = next == null ? 0 : next.ticks;
        pendingPrepared = false;
        fallbackArmedReported = next == null;
        awaitingPendingSample = true;
        prepared = new Selection(Kind.CURRENT, null, null, -1, 0, 0, null);
    }

    public String preparedPendingDenial(long movementTick) {
        if (!pendingPrepared || pending == null) throw new IllegalStateException("pending was not prepared");
        DefaultPlan next = pending.nextDefault;
        if (next == null) return null;
        if (movementTick > Long.MAX_VALUE - pending.ticks
                || next.activationTick != movementTick + pending.ticks)
            return "safe_default_activation_mismatch";
        return null;
    }

    public void rejectPreparedPending(long movementTick) {
        if (!pendingPrepared || pending == null) throw new IllegalStateException("pending was not prepared");
        event("window_missed", movementTick, pending.episode, pending.sequence);
        pending = null;
        appliedPending = null;
        pendingPrepared = false;
        if (fallback == null) holdResponse = false;
        prepared = null;
        preparedTick = -1;
    }

    public boolean rejectInstalled(long sequence) {
        boolean changed = false;
        if (pending != null && pending.sequence == sequence) {
            pending = null;
            pendingPrepared = false;
            if (fallback == null) holdResponse = false;
            prepared = null;
            preparedTick = -1;
            changed = true;
        }
        if (appliedPending != null && appliedPending.sequence == sequence) {
            appliedPending = null;
            awaitingPendingSample = false;
            changed = true;
        }
        if (fallback != null && fallback.sequence == sequence) {
            fallback = null;
            defaultRemaining = 0;
            defaultActive = false;
            holdResponse = false;
            fallbackArmedReported = false;
            prepared = null;
            preparedTick = -1;
            changed = true;
        }
        return changed;
    }

    public boolean holdRejectedResponseForFallback() {
        if (fallback == null) return false;
        holdResponse = true;
        return true;
    }

    public void cancelAll() {
        pending = null;
        appliedPending = null;
        fallback = null;
        defaultRemaining = 0;
        defaultActive = false;
        holdResponse = false;
        awaitingPendingSample = false;
        pendingPrepared = false;
        fallbackArmedReported = false;
        prepared = null;
        preparedTick = -1;
    }

    private void revokePermission(long movementTick) {
        if (pending != null) event("window_missed", movementTick, pending.episode, pending.sequence);
        cancelAll();
    }

    public void reset() { cancelAll(); events.clear(); }

    public long pendingSequence() { return pending == null ? -1 : pending.sequence; }
    public boolean ownsSequence(long sequence) {
        return pending != null && pending.sequence == sequence
                || fallback != null && fallback.sequence == sequence;
    }
    public boolean responseReady() { return pending == null && !holdResponse && !awaitingPendingSample; }
    public boolean hasFallback() { return fallback != null; }
    public List<Event> drainEvents() { var result = List.copyOf(events); events.clear(); return result; }

    public void notApplied(long movementTick, String episode, long sequence) {
        requireIdentity(episode, sequence);
        if (movementTick < 0) throw new IllegalArgumentException("invalid not-applied tick");
        event("not_applied", movementTick, episode, sequence);
    }

    private Selection neutral() { return new Selection(Kind.NEUTRAL, new Movement(0,0,false,false,false), null,-1,0,0,null); }
    private void event(String status, long tick, String episode, long sequence) {
        if (events.size() >= 64) throw new IllegalStateException("timing event overflow");
        events.add(new Event(status, tick, episode, sequence));
    }
    private static void requireIdentity(String episode, long sequence) {
        if (episode == null || !episode.matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}") || sequence < 0)
            throw new IllegalArgumentException("invalid timing identity");
    }
    private static void validateDefault(long activationTick, Movement movement, int ticks, long wallBudgetNs) {
        if (activationTick < 0 || movement == null || ticks < 1 || ticks > 20
                || wallBudgetNs < ticks * 50_000_000L || wallBudgetNs > 30_000_000_000L)
            throw new IllegalArgumentException("invalid safe default");
    }
}
