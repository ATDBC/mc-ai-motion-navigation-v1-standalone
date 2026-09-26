package com.mc2p.surface;

import com.mc2p.observation.ClientBlockObservationV3;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.client.MinecraftClient;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;

/** Incremental same-call surface visibility for the formal Fabric observer. */
public final class SurfaceSensor implements ClientBlockObservationV3.SurfaceProvider, AutoCloseable {
    private static final int MAX_BLOCKS = 25_000;
    private static final int MAX_BOXES = 100_000;
    private Object world;
    private SurfaceWorldSource source;
    private TileGeometryStore store;
    private long scene;
    private Geometry geometry;

    private final ByteBuffer boxes = CacheBridge.direct(MAX_BOXES * 48);
    private final ByteBuffer owners = CacheBridge.direct(MAX_BOXES * 4);
    private final ByteBuffer centers = CacheBridge.direct(MAX_BLOCKS * 24);
    private final ByteBuffer opaque = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer query = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer identities = CacheBridge.direct(MAX_BLOCKS * 16);
    private final ByteBuffer camera = CacheBridge.direct(40);
    private final ByteBuffer output = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer areas = CacheBridge.direct(MAX_BLOCKS * 8);
    private final ByteBuffer times = CacheBridge.direct(16);
    private final ByteBuffer stats = CacheBridge.direct(64);
    private final ByteBuffer preparation = CacheBridge.direct(64);

    private static final class Geometry {
        final ArrayList<int[]> positions = new ArrayList<>();
    }

    @Override
    public void close() {
        DirtyTracker.clear();
        if (scene != 0) {
            CacheBridge.destroy(scene);
            scene = 0;
        }
        world = null;
        source = null;
        store = null;
        geometry = null;
    }

    private Geometry pack() {
        var result = new Geometry();
        boxes.clear(); owners.clear(); centers.clear(); opaque.clear();
        int boxCount = 0;
        for (var block : store.records()) {
            if (block.boxes.length == 0) continue;
            int owner = result.positions.size();
            int addedBoxes = block.boxes.length / 6;
            if (owner >= MAX_BLOCKS || boxCount + addedBoxes > MAX_BOXES)
                throw new IllegalStateException("surface geometry capacity exceeded");
            result.positions.add(new int[]{block.x, block.y, block.z});
            centers.putDouble(block.x + .5).putDouble(block.y + .5).putDouble(block.z + .5);
            opaque.put((byte)(block.opaque ? 1 : 0));
            for (double value : block.boxes) boxes.putDouble(value);
            for (int index = 0; index < addedBoxes; index++) {
                owners.putInt(owner);
                boxCount++;
            }
        }
        CacheBridge.update(scene, boxes, owners, boxCount, centers, opaque,
                result.positions.size(), identities, preparation);
        return result;
    }

    @Override
    public List<BlockPos> visible(MinecraftClient client, Vec3d eye, float yaw, float pitch) {
        if (!client.isOnThread() || client.world == null || client.player == null)
            throw new IllegalStateException("surface sampling requires client thread and body");
        if (world != client.world) {
            close();
            scene = CacheBridge.create(4);
            world = client.world;
            source = new SurfaceWorldSource(client);
            store = new TileGeometryStore(4, source, client.world.getBottomY(), client.world.getTopY());
            DirtyTracker.bind(world, new DirtyTracker.Listener() {
                public void block(int x, int y, int z) { store.blockChanged(x, y, z); }
                public void chunk(int x, int z) { store.chunkChanged(x, z); }
            });
        }
        source.begin();
        store.refresh(eye.x, eye.y, eye.z);
        if (geometry == null || store.changed) geometry = pack();
        int count = geometry.positions.size();
        camera.putDouble(0, eye.x).putDouble(8, eye.y).putDouble(16, eye.z)
                .putDouble(24, -yaw).putDouble(32, pitch);
        query.clear();
        for (var position : geometry.positions) {
            double dx = Math.max(Math.max(position[0] - eye.x, eye.x - position[0] - 1), 0);
            double dy = Math.max(Math.max(position[1] - eye.y, eye.y - position[1] - 1), 0);
            double dz = Math.max(Math.max(position[2] - eye.z, eye.z - position[2] - 1), 0);
            query.put((byte)(dx * dx + dy * dy + dz * dz <= 256 ? 1 : 0));
        }
        CacheBridge.framePose(scene, camera, query, count, output, areas, times, stats);
        var visible = new ArrayList<BlockPos>();
        for (int index = 0; index < count; index++) if (output.get(index) != 0) {
            var position = geometry.positions.get(index);
            visible.add(new BlockPos(position[0], position[1], position[2]));
        }
        return List.copyOf(visible);
    }
}
