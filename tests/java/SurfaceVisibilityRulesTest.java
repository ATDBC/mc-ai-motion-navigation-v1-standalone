import com.mc2p.surface.SurfaceVisibilityRules;

public final class SurfaceVisibilityRulesTest {
    private static void expect(
            String id, boolean invisible, boolean empty, String fluid,
            boolean included, boolean occludes) {
        var actual = SurfaceVisibilityRules.classify(id, invisible, empty, fluid);
        if (actual.included() != included || actual.occludes() != occludes) {
            throw new AssertionError(id + " classified as " + actual);
        }
    }

    public static void main(String[] args) {
        expect("minecraft:glass", false, false, null, true, false);
        expect("minecraft:red_stained_glass", false, false, null, true, false);
        expect("minecraft:glass_pane", false, false, null, true, false);
        expect("minecraft:cobweb", false, false, null, true, false);
        expect("minecraft:short_grass", false, false, null, true, false);
        expect("minecraft:iron_bars", false, false, null, true, true);
        expect("minecraft:oak_fence", false, false, null, true, true);
        expect("minecraft:oak_trapdoor", false, false, null, true, true);
        expect("minecraft:blue_ice", false, false, null, true, true);
        expect("minecraft:frosted_ice", false, false, null, true, true);
        expect("minecraft:oak_leaves", false, false, null, true, true);
        expect("minecraft:barrier", true, false, null, false, false);
        expect("minecraft:light", true, true, null, false, false);
        expect("minecraft:water", true, true, "minecraft:water", true, false);
        expect("minecraft:lava", true, true, "minecraft:lava", true, true);
        // A waterlogged solid keeps the solid's rule; water does not make it transparent.
        expect("minecraft:stone_slab", false, false, "minecraft:water", true, true);
        expect("minecraft:oak_stairs", false, false, "minecraft:water", true, true);
        System.out.println("SURFACE_VISIBILITY_RULES_OK");
    }
}
