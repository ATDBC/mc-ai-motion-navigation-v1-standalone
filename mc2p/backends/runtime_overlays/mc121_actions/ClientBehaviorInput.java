package com.mc2p.actions;

import java.util.function.BooleanSupplier;
import java.util.function.LongSupplier;
import net.minecraft.client.input.Input;

/** Robot-owned input values. Vanilla still consumes movement, collision, jump and slowdown. */
public final class ClientBehaviorInput extends Input {
    public record Sample(String episodeId, long requestSequenceId, long movementTickId,
            long sampledAtJvmNs, String state, float forward, float strafe,
            boolean jump, boolean sneak, boolean sprint) {}
    @FunctionalInterface public interface SampleObserver { void accept(Sample sample); }
    @FunctionalInterface public interface ActivationObserver { boolean activate(Object payload); }
    private static final SampleObserver NO_DIAGNOSTICS = sample -> {};
    private static final Runnable NO_BEFORE_SAMPLE = () -> {};
    private static final ActivationObserver NO_ACTIVATION = payload -> true;
    private final ClientRequestGate gate;
    private final LongSupplier clock;
    private final LongSupplier applicationClock;
    private final BooleanSupplier allowed;
    private final Runnable beforeSample;
    private final SampleObserver observer;
    private final ActivationObserver activationObserver;
    private java.util.function.Consumer<String> timingRejectionObserver = reason -> {};
    private final ClientTimingSchedule timing;
    private int forward, strafe;
    private boolean jump, sneak, sprint, appliedSprint;
    private long samples, leasedSamples, droppedSamples;
    private String acceptedEpisode;
    private long acceptedSequence = -1;
    private Sample lastSample;
    private final java.util.ArrayList<Sample> pendingSamples = new java.util.ArrayList<>();

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed) {
        this(gate, clock, allowed, clock, NO_BEFORE_SAMPLE, NO_ACTIVATION, NO_DIAGNOSTICS);
    }

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed,
                               SampleObserver observer) {
        this(gate, clock, allowed, clock, NO_BEFORE_SAMPLE, NO_ACTIVATION, observer);
    }

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed,
                               LongSupplier applicationClock, SampleObserver observer) {
        this(gate, clock, allowed, applicationClock, NO_BEFORE_SAMPLE, NO_ACTIVATION, observer);
    }

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed,
                               LongSupplier applicationClock, Runnable beforeSample,
                               SampleObserver observer) {
        this(gate, clock, allowed, applicationClock, beforeSample, NO_ACTIVATION, observer);
    }

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed,
                               LongSupplier applicationClock, Runnable beforeSample,
                               ActivationObserver activationObserver, SampleObserver observer) {
        this.gate = gate;
        this.clock = java.util.Objects.requireNonNull(clock);
        this.allowed = allowed;
        this.applicationClock = java.util.Objects.requireNonNull(applicationClock);
        this.beforeSample = java.util.Objects.requireNonNull(beforeSample);
        this.activationObserver = java.util.Objects.requireNonNull(activationObserver);
        this.observer = java.util.Objects.requireNonNull(observer);
        this.timing = new ClientTimingSchedule(this.clock, this.allowed);
    }

    public void bindAcceptedRequest(String episode, long sequence) {
        gate.requireOwnerThread();
        if (episode == null || sequence < 0) throw new IllegalArgumentException("invalid accepted request identity");
        acceptedEpisode = episode;
        acceptedSequence = sequence;
    }

    public void bindDispatchedRequest(String episode, long sequence, String status) {
        gate.requireOwnerThread();
        if ("executed".equals(status) || "confirmed_local".equals(status)
                || "pending_confirmation".equals(status)
                || "operation_rejected".equals(status)) {
            bindAcceptedRequest(episode, sequence);
        } else if (!"rejected".equals(status)) {
            throw new IllegalArgumentException("invalid dispatched request status");
        }
    }

    public void rejectAdmittedRequest(long sequence) {
        gate.requireOwnerThread();
        if (gate.cancel(sequence)) {
            clear();
            if (acceptedSequence == sequence) {
                acceptedEpisode = null;
                acceptedSequence = -1;
            }
        }
    }

    public void set(int forward, int strafe, boolean jump, boolean sneak, boolean sprint) {
        gate.requireOwnerThread();
        this.forward = forward; this.strafe = strafe;
        this.jump = jump; this.sneak = sneak; this.sprint = sprint;
    }

    public void clear() {
        gate.requireOwnerThread();
        forward = strafe = 0;
        jump = sneak = sprint = appliedSprint = false;
        movementForward = movementSideways = 0;
        pressingForward = pressingBack = pressingLeft = pressingRight = jumping = sneaking = false;
    }

    public void clearTiming() { timing.cancelAll(); }

    public boolean rejectTimingForRequest(long sequence, boolean installedThisCall) {
        gate.requireOwnerThread();
        return installedThisCall && timing.rejectInstalled(sequence);
    }

    public boolean holdRejectedResponseForFallback() {
        gate.requireOwnerThread();
        return timing.holdRejectedResponseForFallback();
    }

    public void setTimingRejectionObserver(java.util.function.Consumer<String> observer) {
        gate.requireOwnerThread();
        timingRejectionObserver = java.util.Objects.requireNonNull(observer);
    }

    public void armDefault(String episode, long sequence, long activationTick,
                           int forward, int strafe, boolean jump, boolean sneak, boolean sprint,
                           int ticks, long wallBudgetNs) {
        gate.requireOwnerThread();
        timing.armDefault(episode, sequence, activationTick,
                new ClientTimingSchedule.Movement(forward, strafe, jump, sneak, sprint),
                ticks, wallBudgetNs);
    }

    public void stageWindowed(String episode, long sequence, long admittedAtTick, long earliest, long latest,
                              long deadlineNs, int ticks, int forward, int strafe,
                              boolean jump, boolean sneak, boolean sprint, Object activationPayload,
                              ClientTimingSchedule.DefaultPlan nextDefault) {
        gate.requireOwnerThread();
        timing.admitPending(episode, sequence, admittedAtTick, earliest, latest, deadlineNs, ticks,
                new ClientTimingSchedule.Movement(forward, strafe, jump, sneak, sprint), activationPayload,
                nextDefault);
    }

    public boolean timingResponseReady() { return timing.responseReady(); }
    public boolean timingOwns(long sequence) { return timing.ownsSequence(sequence); }
    public java.util.List<ClientTimingSchedule.Event> drainTimingEvents() { return timing.drainEvents(); }

    public void beginSnapshot() {
        // Clear desired state on admission, even if the new operation is later rejected.
        // Keep the previous sample until vanilla consumes its edges and calls Input.tick.
        acceptedEpisode = null;
        acceptedSequence = -1;
        set(0, 0, false, false, false);
    }

    public void prepareForPlayerTick() {
        gate.requireOwnerThread();
        // Operations coupled to movement run immediately before vanilla samples
        // this same input. They may reject and clear the lease before any movement
        // edge becomes visible to the player simulation.
        beforeSample.run();
        long now = clock.getAsLong();
        long movementTick = samples + 1;
        var selected = timing.prepare(movementTick, gate.leaseActive(now));
        if (selected.kind() == ClientTimingSchedule.Kind.PENDING) {
            String timingDenied = timing.preparedPendingDenial(movementTick);
            if (timingDenied != null) {
                timing.rejectPreparedPending(movementTick);
                timingRejectionObserver.accept(timingDenied);
                selected = timing.prepare(movementTick, false);
                applyDefaultSelection(selected);
            } else if (gate.activatePending(selected.episodeId(), selected.requestSequenceId(),
                    selected.deadlineNs(), selected.validForTicks(), now)) {
                if (activationObserver.activate(selected.activationPayload())) {
                    timing.confirmPendingActivation();
                    bindAcceptedRequest(selected.episodeId(), selected.requestSequenceId());
                    var movement = selected.movement();
                    set(movement.forward(), movement.strafe(), movement.jump(), movement.sneak(), movement.sprint());
                } else {
                    gate.cancel(selected.requestSequenceId());
                    timing.rejectPreparedPending(movementTick);
                    selected = timing.prepare(movementTick, false);
                    applyDefaultSelection(selected);
                }
            } else {
                timing.rejectPreparedPending(movementTick);
                timingRejectionObserver.accept("window_activation_rejected");
                selected = timing.prepare(movementTick, false);
                applyDefaultSelection(selected);
            }
        } else if (selected.kind() == ClientTimingSchedule.Kind.DEFAULT) {
            applyDefaultSelection(selected);
        } else if (selected.kind() == ClientTimingSchedule.Kind.NEUTRAL) {
            clear();
            if (!allowed.getAsBoolean()) timingRejectionObserver.accept("permission_revoked");
        }
        // Semantic sneak is a current, admitted input level. Vanilla reads it
        // before Input.tick to decide legal crouching and its attribute factor.
        // Without this projection a one-sample lease separated by idle samples
        // repeatedly moves on the unslowed first-press frame. Do not pre-sample
        // movement/jump, release prior edges, or consume/extend the lease here.
        boolean timed = selected.kind() == ClientTimingSchedule.Kind.DEFAULT;
        sneaking = sneak && allowed.getAsBoolean() && (timed || gate.leaseActive(now));
    }

    private void applyDefaultSelection(ClientTimingSchedule.Selection selected) {
        if (selected.kind() == ClientTimingSchedule.Kind.DEFAULT) {
            bindAcceptedRequest(selected.episodeId(), selected.requestSequenceId());
            var movement = selected.movement();
            set(movement.forward(), movement.strafe(), movement.jump(), movement.sneak(), movement.sprint());
        } else {
            clear();
        }
    }

    public boolean sprintRequested() { return appliedSprint; }
    /** The exact immutable values returned by the most recent vanilla Input.tick sample. */
    public Sample lastSample() { return lastSample; }
    public long sampleCount() { return samples; }
    public long leasedSampleCount() { return leasedSamples; }
    public long droppedSampleCount() { return droppedSamples; }
    public long oldestRetainedMovementTick() {
        return pendingSamples.isEmpty() ? 0L : pendingSamples.get(0).movementTickId();
    }
    public java.util.List<Sample> drainSamples() {
        gate.requireOwnerThread();
        var result = java.util.List.copyOf(pendingSamples);
        pendingSamples.clear();
        return result;
    }

    public void expireOnClientTick() {
        gate.requireOwnerThread();
        // Client ticks continue even when player simulation does not. Do not consume a sample,
        // reset request identity, or clear vanilla's previous edge just for logical exhaustion.
        if (!allowed.getAsBoolean()) { timing.cancelAll(); clear(); }
        else if (gate.wallClockExpired(clock.getAsLong()) && !timing.hasFallback()) clear();
    }

    @Override
    public void tick(boolean slowDown, float slowDownFactor) {
        gate.requireOwnerThread();
        samples++;
        long now = clock.getAsLong();
        long sampledAtJvmNs = applicationClock.getAsLong();
        if (sampledAtJvmNs < 0) throw new IllegalStateException("input application clock regressed");
        boolean permitted = allowed.getAsBoolean();
        long movementTick = samples;
        var timedSelection = timing.prepare(movementTick, gate.leaseActive(now));
        boolean defaultActive = timedSelection.kind() == ClientTimingSchedule.Kind.DEFAULT;
        boolean active = permitted && (defaultActive || gate.leaseActive(now));
        String sampleState;
        if (!active) {
            sampleState = !permitted ? "disallowed"
                    : gate.wallClockExpired(now) ? "expired" : "lease_exhausted";
            clear();
        } else {
            pressingForward = forward > 0; pressingBack = forward < 0;
            pressingLeft = strafe > 0; pressingRight = strafe < 0;
            movementForward = forward * (slowDown ? slowDownFactor : 1);
            movementSideways = strafe * (slowDown ? slowDownFactor : 1);
            jumping = jump; sneaking = sneak; appliedSprint = sprint;
            leasedSamples++;
            sampleState = defaultActive ? "default_active" : movementForward == 0 && movementSideways == 0
                    && !jumping && !sneaking && !appliedSprint ? "neutral" : "leased";
            // A held lease is consumed by actual player input sampling, not observation emission.
            if (!defaultActive) gate.endControlTick();
        }
        lastSample = new Sample(acceptedEpisode, acceptedSequence, samples, sampledAtJvmNs, sampleState,
                movementForward, movementSideways, jumping, sneaking, appliedSprint);
        if (pendingSamples.size() >= 64) {
            pendingSamples.remove(0);
            droppedSamples++;
        }
        pendingSamples.add(lastSample);
        observer.accept(lastSample);
        if (active) {
            if (!defaultActive) timing.parentSampled(movementTick, acceptedEpisode, acceptedSequence);
            timing.sampled(movementTick);
        }
    }
}
