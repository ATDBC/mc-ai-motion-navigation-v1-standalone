package com.mc2p.surface;

import java.nio.ByteBuffer;
import java.util.*;

/**
 * Property check for the proposed capacity fix: drop opaque full cubes whose six
 * face neighbours are opaque full cubes, then compare visibility with the
 * unculled scene using the formal CacheBridge at commit 3e0c64b.
 *
 * <p>Build the native core as described in
 * {@code reviews/2026-09-26-d035-repro/com/mc2p/surface/FlatFloorHarness.java}, then:
 *
 * <pre>
 * javac -d /tmp/surface-classes \
 *   mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/CacheBridge.java \
 *   &lt;review-branch&gt;/reviews/2026-09-26-d035-plan-repro/com/mc2p/surface/EnclosedCullingProperty.java
 * # mixed random worlds (density .35-.95; opaque cubes, glass, slabs)
 * java -cp /tmp/surface-classes com.mc2p.surface.EnclosedCullingProperty /tmp/libsurface_cache.so 200
 * # solid rock with a carved 1x2 tunnel, opaque cubes only
 * java -cp /tmp/surface-classes com.mc2p.surface.EnclosedCullingProperty /tmp/libsurface_cache.so 20 1.0 stone
 * </pre>
 *
 * <p>Blocks on the edge of the test region have neighbours outside it, so they
 * are kept, exactly as the store edge would be kept in SurfaceSensor.
 */
public final class EnclosedCullingProperty {
    record Cell(int x, int y, int z, int kind) {} // 1 opaque cube, 2 glass cube, 3 opaque bottom slab

    static Set<List<Integer>> visible(List<Cell> cells, double[] eye, double yaw, double pitch) {
        long scene = CacheBridge.create(4);
        try {
            int n = cells.size();
            ByteBuffer boxes = CacheBridge.direct(Math.max(1, n) * 48), owners = CacheBridge.direct(Math.max(1, n) * 4),
                centers = CacheBridge.direct(Math.max(1, n) * 24), opaque = CacheBridge.direct(Math.max(1, n)),
                ids = CacheBridge.direct(Math.max(1, n) * 16), st = CacheBridge.direct(64);
            for (int i = 0; i < n; i++) {
                Cell c = cells.get(i);
                double top = c.kind() == 3 ? c.y() + .5 : c.y() + 1;
                for (double v : new double[]{c.x(), c.y(), c.z(), c.x() + 1, top, c.z() + 1}) boxes.putDouble(v);
                owners.putInt(i);
                centers.putDouble(c.x() + .5).putDouble(c.y() + .5).putDouble(c.z() + .5);
                opaque.put((byte) (c.kind() == 2 ? 0 : 1));
            }
            CacheBridge.update(scene, boxes, owners, n, centers, opaque, n, ids, st);
            ByteBuffer camera = CacheBridge.direct(40), query = CacheBridge.direct(Math.max(1, n)),
                out = CacheBridge.direct(Math.max(1, n)), areas = CacheBridge.direct(Math.max(1, n) * 8),
                times = CacheBridge.direct(16), stats = CacheBridge.direct(64);
            camera.putDouble(0, eye[0]).putDouble(8, eye[1]).putDouble(16, eye[2]).putDouble(24, -yaw).putDouble(32, pitch);
            for (int i = 0; i < n; i++) query.put((byte) 1);
            CacheBridge.framePose(scene, camera, query, n, out, areas, times, stats);
            Set<List<Integer>> result = new HashSet<>();
            for (int i = 0; i < n; i++) if (out.get(i) != 0) result.add(List.of(cells.get(i).x(), cells.get(i).y(), cells.get(i).z()));
            return result;
        } finally {
            CacheBridge.destroy(scene);
        }
    }

    public static void main(String[] args) {
        System.load(args[0]);
        Random random = new Random(20260926L);
        int worlds = Integer.parseInt(args[1]);
        long keptTotal = 0, allTotal = 0; int mismatches = 0, leaks = 0;
        for (int w = 0; w < worlds; w++) {
            double density = args.length > 2 ? Double.parseDouble(args[2]) : new double[]{.35, .6, .85, .95}[w % 4];
            Map<List<Integer>, Cell> map = new HashMap<>();
            for (int x = -9; x <= 9; x++) for (int y = -5; y <= 6; y++) for (int z = -9; z <= 9; z++) {
                if (Math.abs(x) <= 1 && Math.abs(z) <= 1 && (y == 0 || y == 1)) continue; // eye pocket
                if (random.nextDouble() >= density) continue;
                double r = args.length > 3 ? 0 : random.nextDouble();
                int kind = r < .85 ? 1 : r < .93 ? 2 : 3;
                map.put(List.of(x, y, z), new Cell(x, y, z, kind));
            }
            if (density >= .85) { // carve a tunnel so dense worlds still have views
                int axis = random.nextInt(2);
                for (int t = -9; t <= 9; t++) for (int y = 0; y <= 1; y++)
                    map.remove(axis == 0 ? List.of(t, y, 0) : List.of(0, y, t));
            }
            List<Cell> all = new ArrayList<>(map.values());
            List<Cell> kept = new ArrayList<>();
            for (Cell c : all) {
                boolean enclosed = c.kind() == 1;
                for (int[] d : new int[][]{{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}}) {
                    Cell n = map.get(List.of(c.x() + d[0], c.y() + d[1], c.z() + d[2]));
                    if (n == null || n.kind() != 1) { enclosed = false; break; }
                }
                if (!enclosed) kept.add(c);
            }
            double[] eye = {random.nextDouble() * 2 - .5, 1.62 - 1 + random.nextDouble() * .3, random.nextDouble() * 2 - .5};
            double yaw = random.nextDouble() * 360 - 180, pitch = random.nextDouble() * 120 - 60;
            Set<List<Integer>> vAll = visible(all, eye, yaw, pitch);
            Set<List<Integer>> vKept = visible(kept, eye, yaw, pitch);
            Set<List<Integer>> keptKeys = new HashSet<>();
            for (Cell c : kept) keptKeys.add(List.of(c.x(), c.y(), c.z()));
            Set<List<Integer>> expected = new HashSet<>(vAll); expected.retainAll(keptKeys);
            if (!expected.equals(vKept)) mismatches++;
            for (List<Integer> p : vAll) if (!keptKeys.contains(p)) leaks++;
            keptTotal += kept.size(); allTotal += all.size();
        }
        System.out.printf("worlds=%d mean blocks=%d mean kept=%d visible-set mismatches=%d dropped-but-visible=%d%n",
            worlds, allTotal / worlds, keptTotal / worlds, mismatches, leaks);
    }
}
