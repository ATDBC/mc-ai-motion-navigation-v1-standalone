package com.mc2p.actions;

import java.util.function.BooleanSupplier;
import java.util.function.LongSupplier;
import net.minecraft.client.input.Input;

/** Robot-owned input values. Vanilla still consumes movement, collision, jump and slowdown. */
public final class ClientBehaviorInput extends Input {
    public record Sample(String episodeId, long requestSequenceId,
            long sampledAtJvmNs, String state, float forward, float strafe,
            boolean jump, boolean sneak, boolean sprint) {}
    @FunctionalInterface public interface SampleObserver { void accept(Sample sample); }
    private static final SampleObserver NO_DIAGNOSTICS = sample -> {};
    private final ClientRequestGate gate;
    private final LongSupplier clock;
    private final BooleanSupplier allowed;
    private final SampleObserver observer;
    private int forward, strafe;
    private boolean jump, sneak, sprint, appliedSprint;
    private long samples, leasedSamples;
    private String acceptedEpisode;
    private long acceptedSequence = -1;
    private Sample lastSample;

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed) {
        this(gate, clock, allowed, NO_DIAGNOSTICS);
    }

    public ClientBehaviorInput(ClientRequestGate gate, LongSupplier clock, BooleanSupplier allowed,
                               SampleObserver observer) {
        this.gate = gate;
        this.clock = clock;
        this.allowed = allowed;
        this.observer = java.util.Objects.requireNonNull(observer);
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
                || "pending_confirmation".equals(status)) {
            bindAcceptedRequest(episode, sequence);
        } else if (!"rejected".equals(status)) {
            throw new IllegalArgumentException("invalid dispatched request status");
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

    public void beginSnapshot() {
        // Clear desired state on admission, even if the new operation is later rejected.
        // Keep the previous sample until vanilla consumes its edges and calls Input.tick.
        acceptedEpisode = null;
        acceptedSequence = -1;
        set(0, 0, false, false, false);
    }

    public void prepareForPlayerTick() {
        gate.requireOwnerThread();
        // Semantic sneak is a current, admitted input level. Vanilla reads it
        // before Input.tick to decide legal crouching and its attribute factor.
        // Without this projection a one-sample lease separated by idle samples
        // repeatedly moves on the unslowed first-press frame. Do not pre-sample
        // movement/jump, release prior edges, or consume/extend the lease here.
        if (sneak && allowed.getAsBoolean() && gate.leaseActive(clock.getAsLong())) sneaking = true;
    }

    public boolean sprintRequested() { return appliedSprint; }
    /** The exact immutable values returned by the most recent vanilla Input.tick sample. */
    public Sample lastSample() { return lastSample; }
    public long sampleCount() { return samples; }
    public long leasedSampleCount() { return leasedSamples; }

    public void expireOnClientTick() {
        gate.requireOwnerThread();
        // Client ticks continue even when player simulation does not. Do not consume a sample,
        // reset request identity, or clear vanilla's previous edge just for logical exhaustion.
        if (!allowed.getAsBoolean() || gate.wallClockExpired(clock.getAsLong())) clear();
    }

    @Override
    public void tick(boolean slowDown, float slowDownFactor) {
        gate.requireOwnerThread();
        samples++;
        long now = clock.getAsLong();
        boolean permitted = allowed.getAsBoolean();
        boolean active = permitted && gate.leaseActive(now);
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
            sampleState = movementForward == 0 && movementSideways == 0
                    && !jumping && !sneaking && !appliedSprint ? "neutral" : "leased";
            // A held lease is consumed by actual player input sampling, not observation emission.
            gate.endControlTick();
        }
        lastSample = new Sample(acceptedEpisode, acceptedSequence, now, sampleState,
                movementForward, movementSideways, jumping, sneaking, appliedSprint);
        observer.accept(lastSample);
    }
}
