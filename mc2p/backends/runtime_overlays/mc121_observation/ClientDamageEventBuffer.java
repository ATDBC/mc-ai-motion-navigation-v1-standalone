package com.mc2p.observation;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.function.IntFunction;

/** Client-thread-only bounded packet facts with next-generation acknowledgement. */
public final class ClientDamageEventBuffer {
    public static final int MAX_EVENTS = 64;

    private final ArrayDeque<Pending> pending = new ArrayDeque<>();
    private Object world;
    private long nextSequence;
    private long lastSnapshotGeneration = -1;
    private int droppedCount;

    public void record(Object currentWorld, long worldTick, int targetEntityId,
                       String damageType, int sourceEntityId,
                       int directSourceEntityId) {
        if (currentWorld == null || worldTick < 0 || targetEntityId < 0
                || damageType == null || damageType.isBlank()
                || sourceEntityId < -1 || directSourceEntityId < -1) {
            throw new IllegalArgumentException("invalid damage event");
        }
        if (world != currentWorld) clearForWorld(currentWorld);
        if (pending.size() == MAX_EVENTS) {
            pending.removeFirst();
            droppedCount = Math.incrementExact(droppedCount);
        }
        nextSequence = Math.incrementExact(nextSequence);
        pending.addLast(new Pending(
                nextSequence, worldTick, targetEntityId, damageType,
                sourceEntityId, directSourceEntityId));
    }

    public Snapshot snapshot(Object currentWorld, long generation, int selfEntityId,
                             IntFunction<String> trackIdLookup) {
        if (currentWorld == null || generation < 0 || selfEntityId < 0
                || trackIdLookup == null) {
            throw new IllegalArgumentException("invalid damage event snapshot");
        }
        if (world != currentWorld) clearForWorld(currentWorld);
        if (lastSnapshotGeneration >= 0 && generation < lastSnapshotGeneration) {
            clearForWorld(currentWorld);
        } else if (generation > lastSnapshotGeneration) {
            pending.removeIf(event -> event.deliveredGeneration >= 0
                    && event.deliveredGeneration < generation);
        }
        lastSnapshotGeneration = generation;

        ArrayList<DamageEvent> events = new ArrayList<>();
        for (Pending event : pending) {
            boolean targetIsSelf = event.targetEntityId == selfEntityId;
            String targetRef = targetIsSelf
                    ? null : trackIdLookup.apply(event.targetEntityId);
            if (!targetIsSelf && targetRef == null) continue;
            boolean sourcePresent = event.sourceEntityId >= 0;
            boolean sourceIsSelf = sourcePresent
                    && event.sourceEntityId == selfEntityId;
            boolean directPresent = event.directSourceEntityId >= 0;
            boolean directIsSelf = directPresent
                    && event.directSourceEntityId == selfEntityId;
            events.add(new DamageEvent(
                    event.sequence, event.worldTick, targetIsSelf, targetRef,
                    event.damageType, sourcePresent, sourceIsSelf,
                    sourcePresent && !sourceIsSelf
                            ? trackIdLookup.apply(event.sourceEntityId) : null,
                    directPresent, directIsSelf,
                    directPresent && !directIsSelf
                            ? trackIdLookup.apply(event.directSourceEntityId) : null));
            event.deliveredGeneration = generation;
        }
        return new Snapshot(List.copyOf(events), droppedCount);
    }

    public void clear() {
        pending.clear();
        world = null;
        lastSnapshotGeneration = -1;
        droppedCount = 0;
    }

    private void clearForWorld(Object currentWorld) {
        pending.clear();
        world = currentWorld;
        lastSnapshotGeneration = -1;
        droppedCount = 0;
        // Sequence never resets, so stale events cannot alias after a world change.
    }

    private static final class Pending {
        final long sequence;
        final long worldTick;
        final int targetEntityId;
        final String damageType;
        final int sourceEntityId;
        final int directSourceEntityId;
        long deliveredGeneration = -1;

        Pending(long sequence, long worldTick, int targetEntityId,
                String damageType, int sourceEntityId, int directSourceEntityId) {
            this.sequence = sequence;
            this.worldTick = worldTick;
            this.targetEntityId = targetEntityId;
            this.damageType = damageType;
            this.sourceEntityId = sourceEntityId;
            this.directSourceEntityId = directSourceEntityId;
        }
    }

    public record DamageEvent(
            long eventSequenceId,
            long worldTick,
            boolean targetIsSelf,
            String targetEntityRef,
            String damageType,
            boolean sourceEntityPresent,
            boolean sourceIsSelf,
            String sourceEntityRef,
            boolean directEntityPresent,
            boolean directSourceIsSelf,
            String directSourceEntityRef) {}

    public record Snapshot(List<DamageEvent> events, int droppedCount) {}
}
