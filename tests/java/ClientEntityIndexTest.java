import com.mc2p.observation.ClientEntityIndex;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.util.math.Vec3d;

public final class ClientEntityIndexTest {
    private static Entity entity() { return new ArmorStandEntity(EntityType.ARMOR_STAND, null); }
    private static void require(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }
    public static void main(String[] args) {
        SharedConstants.createGameVersion();
        Bootstrap.initialize();
        ClientEntityIndex index = new ClientEntityIndex();
        Object world = new Object();
        List<Entity> loaded = new ArrayList<>();
        for (int i = 0; i < 65; i++) loaded.add(entity());
        index.beginFrame(world, 0, loaded);
        for (Entity e : loaded) index.offer(e, new Vec3d(0, 0, 4), "minecraft:armor_stand");
        var first = index.selected();
        require(first.size() == 64 && index.truncatedCount() == 1, "65-to-64 bound/count");
        Entity firstEntity = loaded.getFirst();
        String firstId = first.stream().filter(candidate -> candidate.entity() == firstEntity)
                .findFirst().orElseThrow().trackId();
        require(firstId.equals(index.trackId(firstEntity)), "read-only lookup changed visible identity");
        require(index.resolveLoaded(firstId) == firstEntity, "visible identity did not resolve");
        require(index.trackId(entity()) == null, "read-only lookup created an unknown identity");
        require(index.resolveLoaded("entity-never-seen") == null, "unknown identity resolved");
        var ids = first.stream().map(ClientEntityIndex.Candidate::trackId).toList();
        require(ids.equals(ids.stream().sorted().toList()), "tertiary lexical sort including ids > 9");
        require(index.retainedCount() == 65, "observed identity retention");
        Collections.reverse(loaded);
        index.beginFrame(world, 1, loaded);
        for (Entity e : loaded) index.offer(e, new Vec3d(0, 0, 4), "minecraft:armor_stand");
        require(first.equals(index.selected()), "same ties must survive candidate iteration order");
        index.beginFrame(world, 2, loaded);
        require(index.selected().isEmpty() && index.truncatedCount() == 0, "hidden objects not emitted");
        require(index.retainedCount() == 65, "loaded hidden identities remain");
        require(firstId.equals(index.trackId(firstEntity)), "hidden loaded identity became unavailable");
        require(index.resolveLoaded(firstId) == firstEntity, "hidden loaded identity did not resolve");
        require(index.retainedCount() == 65, "read-only reverse lookup changed retention");
        index.beginFrame(world, 3, loaded);
        for (Entity e : loaded) index.offer(e, new Vec3d(0, 0, 4), "minecraft:armor_stand");
        require(first.equals(index.selected()), "reappeared object identity changed");
        Entity near = entity(); loaded.add(near);
        index.beginFrame(world, 4, loaded);
        for (Entity e : loaded) index.offer(e, new Vec3d(0, 0, e == near ? 1 : 4), "minecraft:armor_stand");
        require(index.selected().getFirst().entity() == near && index.truncatedCount() == 2,
                "nearest must displace worst candidate");
        String nearId = index.selected().getFirst().trackId();
        index.beginFrame(world, 5, List.of(near));
        require(index.retainedCount() == 1, "unloaded references must be reclaimed before filtering");
        index.offer(near, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(index.selected().getFirst().trackId().equals(nearId), "retained object changed");
        near.setRemoved(Entity.RemovalReason.UNLOADED_TO_CHUNK);
        index.beginFrame(world, 6, List.of(near));
        require(index.retainedCount() == 0, "removed-but-still-listed object retained");
        require(index.trackId(near) == null, "removed identity remained addressable");
        require(index.resolveLoaded(nearId) == null, "removed identity remained reverse-addressable");
        Entity replacement = entity();
        index.beginFrame(world, 7, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        String replacementId = index.selected().getFirst().trackId();
        require(!nearId.equals(replacementId), "reloaded object reuses stale id");
        index.beginFrame(world, 0, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        String resetId = index.selected().getFirst().trackId();
        require(!replacementId.equals(resetId), "reset reuses old reference");
        require(index.resolveLoaded(replacementId) == null, "reset retained stale reverse identity");
        Object replacementWorld = new Object();
        index.beginFrame(replacementWorld, 1, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(!resetId.equals(index.selected().getFirst().trackId()), "world replacement reuses reference");
        require(index.resolveLoaded(resetId) == null, "world replacement retained stale reverse identity");
        String worldId = index.selected().getFirst().trackId();
        Object rollbackWorld = new Object();
        index.beginFrame(rollbackWorld, 7, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        String beforeRollback = index.selected().getFirst().trackId();
        index.beginFrame(rollbackWorld, 6, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(!beforeRollback.equals(index.selected().getFirst().trackId()), "generation rollback reused reference");
        require(index.resolveLoaded(worldId) == null, "prior world identity survived later frame reset");
        for (int i = 1; i <= 10000; i++) {
            Entity e = entity();
            index.beginFrame(world, i, List.of(e));
            index.offer(e, new Vec3d(0, 0, 1), "minecraft:armor_stand");
            require(index.retainedCount() == 1, "history accumulates across unloads");
        }
        index.clear();
        require(index.retainedCount() == 0 && index.selected().isEmpty(), "clear must drop all refs");
        loaded.clear();
        for (int i = 0; i <= 4096; i++) loaded.add(entity());
        index.beginFrame(world, 0, loaded);
        for (int i = 0; i < 4096; i++) index.offer(loaded.get(i), new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(index.retainedCount() == 4096 && index.selected().size() == 64, "capacity boundary");
        try {
            index.offer(loaded.get(4096), new Vec3d(0, 0, 1), "minecraft:armor_stand");
            throw new AssertionError("capacity overflow silently accepted");
        } catch (IllegalStateException expected) {
            require(expected.getMessage().equals("entity_track_capacity_exceeded"), "stable capacity reason");
        }
        require(index.retainedCount() == 4096, "overflow grew retention");
        try {
            index.selected();
            throw new AssertionError("partial selection exposed after capacity failure");
        } catch (IllegalStateException expected) { }
        index.clear();
        index.beginFrame(world, 0, List.of(replacement));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(index.selected().size() == 1 && index.truncatedCount() == 0, "capacity clear/recovery");
        Entity another = entity();
        index.beginFrame(world, 1, List.of(replacement, another));
        index.offer(replacement, new Vec3d(0, 0, 1), "minecraft:zombie");
        index.offer(another, new Vec3d(0, 0, 1), "minecraft:armor_stand");
        require(index.selected().getFirst().entity() == another, "type must precede track-id tie-break");
        System.out.println("CLIENT_ENTITY_INDEX_OK");
    }
}
