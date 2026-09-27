package com.mc2p.surface;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

/** Removes only full opaque cubes whose six face-neighbours hide every surface. */
public final class SurfaceGeometryCuller {
    @FunctionalInterface
    public interface OcclusionLookup {
        boolean isOccludingFullCube(int x, int y, int z);
    }

    private static final int[][] FACES = {
        {1, 0, 0}, {-1, 0, 0}, {0, 1, 0},
        {0, -1, 0}, {0, 0, 1}, {0, 0, -1},
    };

    private SurfaceGeometryCuller() {}

    public static List<TileGeometryStore.Record> select(
            Collection<TileGeometryStore.Record> records,
            OcclusionLookup lookup) {
        if (records == null || lookup == null) {
            throw new IllegalArgumentException("missing surface culling input");
        }
        var result = new ArrayList<TileGeometryStore.Record>(records.size());
        for (var record : records) {
            boolean enclosed = record.occludingFullCube;
            if (enclosed) {
                for (int[] offset : FACES) {
                    if (!lookup.isOccludingFullCube(
                            record.x + offset[0],
                            record.y + offset[1],
                            record.z + offset[2])) {
                        enclosed = false;
                        break;
                    }
                }
            }
            if (!enclosed) result.add(record);
        }
        return List.copyOf(result);
    }
}
