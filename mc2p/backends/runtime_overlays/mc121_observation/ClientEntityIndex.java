package com.mc2p.observation;

import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.PriorityQueue;
import net.minecraft.entity.Entity;
import net.minecraft.util.math.Vec3d;

/** Client-thread-only lifetime accounting and bounded selection AFTER legality filtering. */
public final class ClientEntityIndex {
    public static final int MAX_RETAINED_REFERENCES = 4096;
    public static final int MAX_VISIBLE_ENTITIES = 64;
    private static final Comparator<Candidate> ORDER = Comparator
            .comparingDouble(Candidate::distanceSquared)
            .thenComparing(Candidate::entityType).thenComparing(Candidate::trackId);
    private final IdentityHashMap<Entity, Track> tracks = new IdentityHashMap<>();
    private final HashMap<String, Entity> entitiesByTrackId = new HashMap<>();
    private final PriorityQueue<Candidate> selected = new PriorityQueue<>(MAX_VISIBLE_ENTITIES, ORDER.reversed());
    // A fresh LOCAL namespace, never read from an entity or network packet.
    private final String namespace = java.util.UUID.randomUUID().toString();
    private long nextId;
    private Object world;
    private long lastGeneration = -1;
    private int visibleCount;
    private boolean failed;

    public void beginFrame(Object currentWorld, long generation, Iterable<Entity> loaded) {
        if (currentWorld == null || generation < 0) throw new IllegalArgumentException("invalid entity frame");
        if (generation == 0 || currentWorld != world || generation < lastGeneration) clear();
        world = currentWorld;
        lastGeneration = generation;
        selected.clear();
        visibleCount = 0;
        failed = false;
        for (Track track : tracks.values()) { track.loaded = false; track.offered = false; }
        // No second collection of all loaded entities: only mark existing bounded entries.
        for (Entity entity : loaded) {
            Track track = tracks.get(entity);
            if (track != null && !entity.isRemoved()) track.loaded = true;
        }
        for (Iterator<java.util.Map.Entry<Entity, Track>> iterator = tracks.entrySet().iterator();
                iterator.hasNext();) {
            var entry = iterator.next();
            if (!entry.getValue().loaded) {
                entitiesByTrackId.remove(entry.getValue().id);
                iterator.remove();
            }
        }
    }

    public void offer(Entity entity, Vec3d relative, String type) {
        requireFrame();
        if (entity.isRemoved()) throw new IllegalArgumentException("removed entity candidate");
        Track track = tracks.get(entity);
        if (track == null) {
            if (tracks.size() == MAX_RETAINED_REFERENCES) {
                failed = true;
                throw new IllegalStateException("entity_track_capacity_exceeded");
            }
            nextId = Math.incrementExact(nextId);
            track = new Track("entity-" + namespace + "-" + nextId);
            tracks.put(entity, track);
            entitiesByTrackId.put(track.id, entity);
        }
        // New tracks are created after beginFrame's loaded scan. Offering the
        // entity is the current-frame proof that the retained reference is loaded.
        track.loaded = true;
        if (track.offered) throw new IllegalStateException("duplicate entity candidate");
        track.offered = true;
        visibleCount = Math.incrementExact(visibleCount);
        Candidate candidate = new Candidate(entity, relative, relative.lengthSquared(), type, track.id);
        if (selected.size() < MAX_VISIBLE_ENTITIES) selected.add(candidate);
        else if (ORDER.compare(candidate, selected.peek()) < 0) {
            selected.remove();
            selected.add(candidate);
        }
    }

    public List<Candidate> selected() {
        if (failed) throw new IllegalStateException("entity_track_capacity_exceeded");
        return selected.stream().sorted(ORDER).toList();
    }

    public int truncatedCount() {
        if (failed) throw new IllegalStateException("entity_track_capacity_exceeded");
        return visibleCount - selected.size();
    }

    public int retainedCount() { return tracks.size(); }

    /** Return an already assigned current-world identity without creating one. */
    public String trackId(Entity entity) {
        Track track = tracks.get(entity);
        return track == null || !track.loaded || entity.isRemoved() ? null : track.id;
    }

    /** Resolve one already assigned, current-world, still-loaded identity without creating state. */
    public Entity resolveLoaded(String trackId) {
        Entity entity = entitiesByTrackId.get(trackId);
        Track track = entity == null ? null : tracks.get(entity);
        return track == null || !track.loaded || entity.isRemoved() ? null : entity;
    }

    public void clear() {
        tracks.clear();
        entitiesByTrackId.clear();
        selected.clear();
        visibleCount = 0;
        world = null;
        lastGeneration = -1;
        failed = false;
        // Never reset the counter: stale references cannot resolve after a new episode.
    }

    private void requireFrame() {
        if (failed) throw new IllegalStateException("entity_track_capacity_exceeded");
        if (world == null) throw new IllegalStateException("entity frame not started");
    }

    private static final class Track {
        final String id;
        boolean loaded;
        boolean offered;
        Track(String id) { this.id = id; }
    }

    public record Candidate(Entity entity, Vec3d relativePosition, double distanceSquared,
                            String entityType, String trackId) {}
}
