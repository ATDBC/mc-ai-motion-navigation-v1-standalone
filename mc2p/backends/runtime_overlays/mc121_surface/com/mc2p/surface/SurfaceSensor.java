package com.mc2p.surface;

import com.mc2p.observation.ClientBlockObservationV3;
import com.mc2p.observation.ClientBlockObservationV3.AirResult;
import com.mc2p.observation.ClientBlockObservationV3.AirStatus;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.client.MinecraftClient;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;

/** Incremental same-call surface visibility for the formal Fabric observer. */
public final class SurfaceSensor implements ClientBlockObservationV3.SurfaceProvider, AutoCloseable {
    private static final int MAX_BLOCKS = 25_000;
    private static final int MAX_BOXES = 100_000;
    private static final int MAX_STORE_RECORDS = 100_000;
    private static final int MAX_ENTITY_BOXES = 256;
    private Object world;
    private SurfaceWorldSource source;
    private TileGeometryStore store;
    private long scene;
    private Geometry geometry;
    private Diagnostics latestDiagnostics = Diagnostics.empty();

    public record Diagnostics(
            long block_read_ns,
            long surface_pack_ns,
            long surface_compute_ns,
            long air_query_ns,
            long block_read_count,
            int store_record_count,
            int packed_block_count,
            int packed_box_count,
            int culled_block_count,
            long halo_read_count,
            boolean cache_rebuilt) {
        static Diagnostics empty() {
            return new Diagnostics(0L, 0L, 0L, 0L, 0L, 0, 0, 0, 0, 0L, false);
        }
    }

    private final ByteBuffer boxes = CacheBridge.direct(MAX_BOXES * 48);
    private final ByteBuffer owners = CacheBridge.direct(MAX_BOXES * 4);
    private final ByteBuffer centers = CacheBridge.direct(MAX_BLOCKS * 24);
    private final ByteBuffer opaque = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer query = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer identities = CacheBridge.direct(MAX_BLOCKS * 16);
    private final ByteBuffer camera = CacheBridge.direct(40);
    private final ByteBuffer output = CacheBridge.direct(MAX_BLOCKS);
    private final ByteBuffer areas = CacheBridge.direct(MAX_BLOCKS * 8);
    private final ByteBuffer times = CacheBridge.direct(24);
    private final ByteBuffer stats = CacheBridge.direct(64);
    private final ByteBuffer preparation = CacheBridge.direct(64);
    private final ByteBuffer entityBoxes = CacheBridge.direct(MAX_ENTITY_BOXES * 48);
    private final ByteBuffer entityVisible = CacheBridge.direct(MAX_ENTITY_BOXES);
    private final ByteBuffer airPositions = CacheBridge.direct(128 * 12);
    private final ByteBuffer airVisible = CacheBridge.direct(128);

    private static final class Geometry {
        final ArrayList<int[]> positions = new ArrayList<>();
        int boxCount;
        int culledBlockCount;
    }

    public Diagnostics diagnostics() { return latestDiagnostics; }

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
        store.beginSurfaceSelection();
        var selected = SurfaceGeometryCuller.select(
                store.records(), store::occludingFullCubeAt);
        result.culledBlockCount = store.records().size() - selected.size();
        for (var block : selected) {
            if (block.boxes.length == 0) continue;
            int owner = result.positions.size();
            int addedBoxes = block.boxes.length / 6;
            if (owner >= MAX_BLOCKS)
                throw new IllegalStateException("surface packed block capacity exceeded");
            if (boxCount + addedBoxes > MAX_BOXES)
                throw new IllegalStateException("surface packed box capacity exceeded");
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
        result.boxCount = boxCount;
        return result;
    }

    @Override
    public List<BlockPos> visible(MinecraftClient client, Vec3d eye, float yaw, float pitch) {
        return sample(client,eye,yaw,pitch,List.of()).visibleBlocks();
    }

    @Override
    public ClientBlockObservationV3.SurfaceFrame sample(
            MinecraftClient client, Vec3d eye, float yaw, float pitch,
            List<BlockPos> airCandidates) {
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
        long blockReadStarted = System.nanoTime();
        source.begin();
        store.refresh(eye.x, eye.y, eye.z);
        if (store.records().size() > MAX_STORE_RECORDS)
            throw new IllegalStateException("surface store record capacity exceeded");
        long blockReadFinished = System.nanoTime();
        boolean rebuilt = geometry == null || store.changed;
        long packStarted = System.nanoTime();
        if (rebuilt) geometry = pack();
        long packFinished = System.nanoTime();
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
        if (airCandidates.size()>128)
            throw new IllegalStateException("surface visual-air candidate budget exceeded");
        var eligibleAirCandidates = new ArrayList<BlockPos>();
        var airResults = new ArrayList<AirResult>();
        for (BlockPos position : airCandidates) {
            if (position.getY() >= client.world.getBottomY()
                    && position.getY() < client.world.getTopY()
                    && client.world.isChunkLoaded(position))
                eligibleAirCandidates.add(position);
            else
                airResults.add(new AirResult(position,AirStatus.UNAVAILABLE));
        }
        airPositions.clear();
        for (BlockPos position : eligibleAirCandidates)
            airPositions.putInt(position.getX()).putInt(position.getY()).putInt(position.getZ());
        long surfaceStarted = System.nanoTime();
        CacheBridge.framePoseAir(scene,camera,query,count,output,areas,times,stats,
                airPositions,eligibleAirCandidates.size(),16.0,airVisible);
        long surfaceFinished = System.nanoTime();
        var visible = new ArrayList<BlockPos>();
        for (int index = 0; index < count; index++) if (output.get(index) != 0) {
            var position = geometry.positions.get(index);
            visible.add(new BlockPos(position[0], position[1], position[2]));
        }
        var visualAir = new ArrayList<BlockPos>();
        for (int index=0;index<eligibleAirCandidates.size();index++) {
            byte status=airVisible.get(index);
            if (status==1) visualAir.add(eligibleAirCandidates.get(index));
            else airResults.add(new AirResult(
                    eligibleAirCandidates.get(index),
                    status==2 ? AirStatus.OUTSIDE_VIEW : AirStatus.OCCLUDED));
        }
        latestDiagnostics = new Diagnostics(
                Math.max(0L, blockReadFinished - blockReadStarted),
                Math.max(0L, packFinished - packStarted),
                Math.max(0L, surfaceFinished - surfaceStarted),
                Math.max(0L,Math.round(times.getDouble(16)*1_000_000.0)),
                store.reads,
                store.records().size(),
                geometry.positions.size(),
                geometry.boxCount,
                geometry.culledBlockCount,
                store.haloReads,
                rebuilt);
        airResults.sort(java.util.Comparator.comparingInt((AirResult result)->result.position().getX())
                .thenComparingInt(result->result.position().getY())
                .thenComparingInt(result->result.position().getZ()));
        return new ClientBlockObservationV3.SurfaceFrame(visible,visualAir,airResults);
    }

    @Override
    public boolean[] visibleBoxes(
            MinecraftClient client, Vec3d eye, float yaw, float pitch, List<Box> candidates) {
        if (!client.isOnThread() || client.world == null || client.player == null)
            throw new IllegalStateException("surface entity sampling requires client thread and body");
        if (world != client.world || scene == 0 || geometry == null)
            throw new IllegalStateException("surface block frame must precede entity sampling");
        if (candidates.size() > MAX_ENTITY_BOXES)
            throw new IllegalStateException("surface entity candidate budget exceeded");
        camera.putDouble(0, eye.x).putDouble(8, eye.y).putDouble(16, eye.z)
                .putDouble(24, -yaw).putDouble(32, pitch);
        entityBoxes.clear();
        for (Box box : candidates) {
            entityBoxes.putDouble(box.minX).putDouble(box.minY).putDouble(box.minZ)
                    .putDouble(box.maxX).putDouble(box.maxY).putDouble(box.maxZ);
        }
        CacheBridge.visibleBoxes(
                scene,camera,entityBoxes,candidates.size(),16.0,entityVisible);
        boolean[] result = new boolean[candidates.size()];
        for (int index = 0; index < result.length; index++)
            result[index] = entityVisible.get(index) != 0;
        return result;
    }
}
