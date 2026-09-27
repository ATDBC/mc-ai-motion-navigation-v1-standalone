import java.util.*;
import com.mc2p.surface.SurfaceGeometryCuller;
import com.mc2p.surface.TileGeometryStore;

public final class SurfaceEnclosedCullingTest {
    private static TileGeometryStore.Record cube(int x, int y, int z) {
        return new TileGeometryStore.Record(
            x, y, z, "minecraft:stone", "stone", true, true, true,
            new double[]{x, y, z, x + 1, y + 1, z + 1}
        );
    }

    private static TileGeometryStore.Record slab(int x, int y, int z) {
        return new TileGeometryStore.Record(
            x, y, z, "minecraft:stone_slab", "slab", true, false, false,
            new double[]{x, y, z, x + 1, y + .5, z + 1}
        );
    }

    public static void main(String[] args) {
        Map<TileGeometryStore.Pos, TileGeometryStore.Record> world = new HashMap<>();
        for (int x = -1; x <= 1; x++) for (int y = -1; y <= 1; y++) {
            for (int z = -1; z <= 1; z++) {
                var record = cube(x, y, z);
                world.put(new TileGeometryStore.Pos(x, y, z), record);
            }
        }
        var selected = SurfaceGeometryCuller.select(
            world.values(), (x, y, z) -> {
                var record = world.get(new TileGeometryStore.Pos(x, y, z));
                return record != null && record.occludingFullCube;
            }
        );
        assert selected.size() == 26 : "only the enclosed center may be removed";
        assert selected.stream().noneMatch(r -> r.x == 0 && r.y == 0 && r.z == 0);

        world.put(new TileGeometryStore.Pos(1, 0, 0), slab(1, 0, 0));
        selected = SurfaceGeometryCuller.select(
            world.values(), (x, y, z) -> {
                var record = world.get(new TileGeometryStore.Pos(x, y, z));
                return record != null && record.occludingFullCube;
            }
        );
        assert selected.size() == 27 : "a non-full neighbor exposes the center";

        world.clear();
        world.put(new TileGeometryStore.Pos(20, 0, 0), cube(20, 0, 0));
        TileGeometryStore store = new TileGeometryStore(
            4, (x, y, z, old) -> world.get(new TileGeometryStore.Pos(x, y, z)),
            -4, 8
        );
        store.refresh(.5, 2, .5);
        assert !store.containsTile(20, 0, 0) : "test coordinate must be outside stored tiles";
        store.beginSurfaceSelection();
        assert store.occludingFullCubeAt(20, 0, 0);
        assert store.occludingFullCubeAt(20, 0, 0);
        assert store.haloReads == 1 : "one-cell halo reads must be cached per packing pass";

        Random random = new Random(21001L);
        for (int trial = 0; trial < 220; trial++) {
            world.clear();
            for (int x = -3; x <= 3; x++) for (int y = -2; y <= 2; y++) {
                for (int z = -3; z <= 3; z++) {
                    if (random.nextDouble() < .72) {
                        var record = random.nextDouble() < .85
                            ? cube(x, y, z) : slab(x, y, z);
                        world.put(new TileGeometryStore.Pos(x, y, z), record);
                    }
                }
            }
            Set<TileGeometryStore.Pos> expected = new HashSet<>();
            for (var entry : world.entrySet()) {
                var p = entry.getKey();
                var r = entry.getValue();
                boolean enclosed = r.occludingFullCube;
                for (int[] d : new int[][]{
                    {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}
                }) {
                    var neighbor = world.get(new TileGeometryStore.Pos(
                        p.x() + d[0], p.y() + d[1], p.z() + d[2]
                    ));
                    enclosed &= neighbor != null && neighbor.occludingFullCube;
                }
                if (!enclosed) expected.add(p);
            }
            var actual = SurfaceGeometryCuller.select(
                world.values(), (x, y, z) -> {
                    var record = world.get(new TileGeometryStore.Pos(x, y, z));
                    return record != null && record.occludingFullCube;
                }
            );
            Set<TileGeometryStore.Pos> actualPositions = new HashSet<>();
            for (var r : actual) actualPositions.add(new TileGeometryStore.Pos(r.x, r.y, r.z));
            assert actualPositions.equals(expected) : "random culling differs from definition";
        }

        List<TileGeometryStore.Record> tunnel = new ArrayList<>();
        Set<TileGeometryStore.Pos> tunnelAir = new HashSet<>();
        for (int z = -17; z <= 17; z++) {
            tunnelAir.add(new TileGeometryStore.Pos(0, 0, z));
            tunnelAir.add(new TileGeometryStore.Pos(0, 1, z));
        }
        for (int x = -17; x <= 17; x++) for (int y = -17; y <= 17; y++) {
            for (int z = -17; z <= 17; z++) {
                if (!tunnelAir.contains(new TileGeometryStore.Pos(x, y, z))) {
                    tunnel.add(cube(x, y, z));
                }
            }
        }
        selected = SurfaceGeometryCuller.select(
            tunnel,
            (x, y, z) -> !tunnelAir.contains(new TileGeometryStore.Pos(x, y, z))
        );
        assert selected.size() < 25_000 : "1x2 tunnel must fit the native block budget";
        assert selected.size() < 400 : "one-cell halo should leave only the tunnel surface";
        System.out.println("SURFACE_ENCLOSED_CULLING_OK");
    }
}
