package com.mc2p.surface;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;

/**
 * Drives the formal {@code com.mc2p.surface.CacheBridge} JNI with a flat floor.
 *
 * <p>Run from the repository root of commit 3e0c64b (Linux shown; the only
 * source change is the {@code __declspec} export macro, passed on the command
 * line):
 *
 * <pre>
 * N=deployment/surface-depth-diagnostic/native; J=$JAVA_HOME
 * g++ -std=c++17 -O2 -shared -fPIC '-D__declspec(x)=__attribute__((visibility("default")))' \
 *   -I$N/vendor/clipper2/include -I$J/include -I$J/include/linux \
 *   $N/cache.cpp $N/jni.cpp $N/vendor/clipper2/src/clipper.engine.cpp -o /tmp/libsurface_cache.so
 * javac -d /tmp/surface-classes \
 *   mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/CacheBridge.java \
 *   &lt;review-branch&gt;/reviews/2026-09-26-d035-repro/com/mc2p/surface/FlatFloorHarness.java
 * java -cp /tmp/surface-classes com.mc2p.surface.FlatFloorHarness /tmp/libsurface_cache.so
 * </pre>
 *
 * <p>The eye is a standing player at (0.5, 101.62, -2.5) looking level along +z,
 * the same pose as the B12-B induced-turn trial.  Opaque flags are chosen here
 * the way {@code SurfaceWorldSource} would choose them.
 */
public final class FlatFloorHarness {
    record Block(int x, int y, int z, double[] box, boolean opaque) {}

    static List<int[]> visible(List<Block> blocks, double ex, double ey, double ez,
                               double mcYaw, double pitch) {
        long scene = CacheBridge.create(4);
        try {
            int n = blocks.size();
            ByteBuffer boxes = CacheBridge.direct(n * 48), owners = CacheBridge.direct(n * 4),
                centers = CacheBridge.direct(n * 24), opaque = CacheBridge.direct(n),
                ids = CacheBridge.direct(n * 16), st = CacheBridge.direct(64);
            for (int i = 0; i < n; i++) {
                Block b = blocks.get(i);
                for (double v : b.box()) boxes.putDouble(v);
                owners.putInt(i);
                centers.putDouble(b.x() + .5).putDouble(b.y() + .5).putDouble(b.z() + .5);
                opaque.put((byte) (b.opaque() ? 1 : 0));
            }
            CacheBridge.update(scene, boxes, owners, n, centers, opaque, n, ids, st);
            ByteBuffer camera = CacheBridge.direct(40), query = CacheBridge.direct(n),
                out = CacheBridge.direct(n), areas = CacheBridge.direct(n * 8),
                times = CacheBridge.direct(16), stats = CacheBridge.direct(64);
            camera.putDouble(0, ex).putDouble(8, ey).putDouble(16, ez)
                  .putDouble(24, -mcYaw).putDouble(32, pitch);
            for (int i = 0; i < n; i++) query.put((byte) 1);
            CacheBridge.framePose(scene, camera, query, n, out, areas, times, stats);
            List<int[]> result = new ArrayList<>();
            for (int i = 0; i < n; i++) if (out.get(i) != 0)
                result.add(new int[]{blocks.get(i).x(), blocks.get(i).y(), blocks.get(i).z()});
            return result;
        } finally {
            CacheBridge.destroy(scene);
        }
    }

    static Block cube(int x, int y, int z, boolean opaque) {
        return new Block(x, y, z, new double[]{x, y, z, x + 1, y + 1, z + 1}, opaque);
    }

    public static void main(String[] args) {
        System.load(args[0]);
        // Flat floor y=99 for x,z in [-12,12]; eye as a standing player at (0.5,100,-2.5).
        List<Block> floor = new ArrayList<>();
        for (int x = -12; x <= 12; x++) for (int z = -14; z <= 14; z++) floor.add(cube(x, 99, z, true));
        double ex = .5, ey = 101.62, ez = -2.5;
        StringBuilder row = new StringBuilder();
        var seen = visible(floor, ex, ey, ez, 0, 0);
        java.util.Set<String> keys = new java.util.HashSet<>();
        for (int[] p : seen) keys.add(p[0] + "," + p[2]);
        System.out.println("level gaze, facing +z: visible floor blocks = " + seen.size());
        for (int z = 14; z >= -6; z--) {
            row.setLength(0);
            for (int x = -12; x <= 12; x++) row.append(x == 0 && z == -3 ? 'R' : keys.contains(x + "," + z) ? 'K' : '.');
            System.out.printf("z=%3d %s%n", z, row);
        }
        StringBuilder column = new StringBuilder();
        for (int z = -3; z <= 14; z++) column.append(keys.contains("0," + z) ? z + " " : "");
        System.out.println("x=0 visible rows: " + column);

        // A 3-high stone wall at z=2 (x -3..3): blocks behind it must stay hidden.
        List<Block> walled = new ArrayList<>(floor);
        for (int x = -3; x <= 3; x++) for (int y = 100; y <= 102; y++) walled.add(cube(x, y, 2, true));
        keys.clear();
        for (int[] p : visible(walled, ex, ey, ez, 0, 0)) if (p[1] == 99) keys.add(p[0] + "," + p[2]);
        column.setLength(0);
        for (int z = -3; z <= 14; z++) column.append(keys.contains("0," + z) ? z + " " : "");
        System.out.println("with opaque wall at z=2: x=0 visible floor rows: " + column);

        // Same wall but marked transparent (as SurfaceWorldSource does for glass, ice, fluids).
        List<Block> glass = new ArrayList<>(floor);
        for (int x = -3; x <= 3; x++) for (int y = 100; y <= 102; y++) glass.add(cube(x, y, 2, false));
        keys.clear();
        for (int[] p : visible(glass, ex, ey, ez, 0, 0)) if (p[1] == 99) keys.add(p[0] + "," + p[2]);
        column.setLength(0);
        for (int z = -3; z <= 14; z++) column.append(keys.contains("0," + z) ? z + " " : "");
        System.out.println("with non-occluding wall at z=2: x=0 visible floor rows: " + column);
    }
}
