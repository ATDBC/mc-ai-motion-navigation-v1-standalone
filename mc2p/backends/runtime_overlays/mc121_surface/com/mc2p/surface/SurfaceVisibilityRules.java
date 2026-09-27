package com.mc2p.surface;

import java.util.Set;

/** One versioned table for profile-4 surface inclusion and occlusion. */
public final class SurfaceVisibilityRules {
    public static final String ID = "surface_visibility_1_21_v1";

    public record Decision(boolean included, boolean occludes) {}

    private static final Set<String> SEE_THROUGH = Set.of(
        "minecraft:glass",
        "minecraft:glass_pane",
        "minecraft:tinted_glass",
        "minecraft:cobweb",
        "minecraft:short_grass",
        "minecraft:tall_grass",
        "minecraft:fern",
        "minecraft:large_fern",
        "minecraft:dead_bush",
        "minecraft:dandelion",
        "minecraft:poppy",
        "minecraft:vine",
        "minecraft:glow_lichen",
        "minecraft:seagrass",
        "minecraft:tall_seagrass",
        "minecraft:kelp",
        "minecraft:kelp_plant",
        "minecraft:sugar_cane"
    );
    private static final Set<String> SEE_THROUGH_EXACT = Set.of(
        "minecraft:ice", "minecraft:packed_ice", "minecraft:blue_ice",
        "minecraft:frosted_ice", "minecraft:wheat", "minecraft:carrots",
        "minecraft:potatoes", "minecraft:beetroots", "minecraft:nether_wart",
        "minecraft:cocoa"
    );
    private SurfaceVisibilityRules() {}

    public static Decision classify(
            String blockId,
            boolean renderInvisible,
            boolean blockShapeEmpty,
            boolean opaqueFullCube,
            String fluidId) {
        if (blockId == null || blockId.isBlank()) {
            throw new IllegalArgumentException("missing block id");
        }
        boolean water = fluidId != null && fluidId.endsWith("water");
        boolean lava = fluidId != null && fluidId.endsWith("lava");
        if (blockShapeEmpty && (water || lava)) {
            return new Decision(true, lava);
        }
        if (renderInvisible) return new Decision(false, false);
        // Minecraft owns this state-sensitive fact. It covers ordinary opaque
        // materials without growing a hand-maintained block-name table.
        if (opaqueFullCube) return new Decision(true, true);
        boolean glass = blockId.endsWith("_stained_glass")
                || blockId.endsWith("_stained_glass_pane");
        boolean plant = blockId.startsWith("minecraft:potted_")
                || blockId.endsWith("_sapling")
                || blockId.endsWith("_tulip")
                || blockId.endsWith("_mushroom")
                || blockId.endsWith("_roots")
                || blockId.endsWith("_fungus")
                || blockId.endsWith("_flower")
                || blockId.endsWith("_bush");
        boolean seeThrough = SEE_THROUGH.contains(blockId)
                || SEE_THROUGH_EXACT.contains(blockId) || glass || plant;
        return new Decision(true, !seeThrough);
    }
}
