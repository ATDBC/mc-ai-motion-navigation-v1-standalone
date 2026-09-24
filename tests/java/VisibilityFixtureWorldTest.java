import java.nio.file.Files;
import java.nio.file.Path;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.block.Block;
import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.boss.dragon.EnderDragonFight;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.item.Items;
import net.minecraft.nbt.*;
import net.minecraft.util.math.ChunkPos;
import net.minecraft.world.World;
import net.minecraft.world.chunk.PalettedContainer;
import net.minecraft.world.storage.RegionFile;
import net.minecraft.world.storage.StorageKey;

/** Decode generated data through pinned vanilla codecs; literal geometry expectations are independent. */
public class VisibilityFixtureWorldTest {
    public static void main(String[] args) throws Exception {
        SharedConstants.createGameVersion();
        Bootstrap.initialize();
        Path destination = Path.of("fixture-native").toAbsolutePath();
        VisibilityFixtureBuilder.create(Path.of(args[0]), destination, 21001L, "mc2p-visibility-native");
        NbtCompound level = NbtIo.readCompressed(destination.resolve("level.dat"), NbtSizeTracker.of(4194304)).getCompound("Data");
        require(level.getInt("DataVersion") == 3953 && level.getInt("GameType") == 0, "wrong version/mode");
        require(level.getString("LevelName").equals("mc2p-visibility-native"), "wrong level identity");
        require(level.getCompound("WorldGenSettings").getLong("seed") == 21001, "wrong seed");
        require(level.getInt("SpawnX") == 0 && level.getInt("SpawnY") == -60 && level.getInt("SpawnZ") == 0,
                "wrong initial spawn");
        require(level.getCompound("GameRules").getString("spawnRadius").equals("0"), "randomized fixture spawn");
        require(!level.contains("Player"), "old player state leaked into fixture");
        require(level.contains("DragonFight", NbtElement.COMPOUND_TYPE), "missing vanilla DragonFight data");
        require(EnderDragonFight.Data.CODEC.parse(NbtOps.INSTANCE, level.get("DragonFight")).getOrThrow()
                .equals(EnderDragonFight.Data.DEFAULT), "nondefault unrelated dragon state");
        NbtCompound origin = readChunk(destination, "region", 0, 0);
        var blocks = decodeGroundSection(origin);
        require(blocks.get(0, 0, 0).isOf(Blocks.BEDROCK), "bedrock floor missing");
        require(blocks.get(0, 3, 0).isOf(Blocks.GRASS_BLOCK), "surface height wrong");
        require(blocks.get(0, 4, 3).isAir(), "initial view wrongly occluded");
        require(blocks.get(1, 4, 3).isOf(Blocks.STONE) && blocks.get(1, 5, 3).isAir(), "partial wall wrong");
        for (int x = 2; x <= 4; x++) for (int y = 4; y <= 7; y++)
            require(blocks.get(x, y, 3).isOf(Blocks.STONE), "full wall has a gap");
        require(blocks.get(5, 4, 3).isAir(), "wall exceeds declared bounds");
        NbtCompound west = readChunk(destination, "region", -1, 0);
        var westBlocks = decodeGroundSection(west);
        require(westBlocks.get(14, 3, 3).isOf(Blocks.DIAMOND_ORE), "exposed ore position wrong");
        require(westBlocks.get(14, 4, 3).isAir(), "ore initially covered");
        require(westBlocks.get(14, 4, 0).isOf(Blocks.CHEST), "chest block missing");
        NbtCompound chest = west.getList("block_entities", NbtElement.COMPOUND_TYPE).getCompound(0);
        NbtCompound stack = chest.getList("Items", NbtElement.COMPOUND_TYPE).getCompound(0);
        require(chest.getString("id").equals("minecraft:chest") && chest.getInt("x") == -2
                && chest.getInt("y") == -60 && chest.getInt("z") == 0, "chest entity position wrong");
        require(stack.getString("id").equals("minecraft:stone") && stack.getInt("count") == 16
                && stack.getByte("Slot") == 0, "normal chest materials absent");
        NbtCompound entityChunk = readChunk(destination, "entities", 0, 0);
        NbtList entities = entityChunk.getList("Entities", NbtElement.COMPOUND_TYPE);
        require(entities.size() == 1, "anchor entity absent or duplicated");
        var entity = EntityType.getEntityFromNbt(entities.getCompound(0), new DetachedTestWorld()).orElseThrow();
        require(entity instanceof ArmorStandEntity, "not a real armor stand");
        ArmorStandEntity stand = (ArmorStandEntity) entity;
        require(stand.getX() == 0.5 && stand.getY() == -60 && stand.getZ() == 6.5, "anchor position wrong");
        require(stand.getCustomName().getString().equals("visible-anchor") && stand.isCustomNameVisible(), "anchor label wrong");
        require(!stand.shouldShowArms() && stand.hasNoGravity(), "anchor pose/motion not fixed");
        require(stand.getEquippedStack(EquipmentSlot.MAINHAND).isOf(Items.DIAMOND_SWORD)
                && stand.getEquippedStack(EquipmentSlot.MAINHAND).getDamage() == 137, "visible hand positive control was not decoded");
        NbtList cows = readChunk(destination, "entities", -1, 0).getList("Entities", NbtElement.COMPOUND_TYPE);
        var cow = EntityType.getEntityFromNbt(cows.getCompound(0), new DetachedTestWorld()).orElseThrow();
        require(cow.getType() == EntityType.COW && cow.getX() == -4.5 && cow.getZ() == 6.5,
                "target-only cow absent");
        require(cow.getCustomName().getString().equals("target-only-name") && !cow.isCustomNameVisible(), "cow label flags wrong");
        require(((LivingEntity) cow).getMainHandStack().isOf(Items.IRON_AXE), "cow hidden-slot negative control absent");
        byte[] original = Files.readAllBytes(destination.resolve("level.dat"));
        try {
            VisibilityFixtureBuilder.create(Path.of(args[0]), destination, 21002L, "mc2p-visibility-replace");
            throw new AssertionError("existing world overwritten");
        } catch (java.nio.file.FileAlreadyExistsException expected) {}
        require(java.util.Arrays.equals(original, Files.readAllBytes(destination.resolve("level.dat"))), "old save changed");
        Path wrongRecipe = Path.of("wrong-version.snbt");
        Files.writeString(wrongRecipe, "{Data:{DataVersion:99999}}");
        try {
            VisibilityFixtureBuilder.create(wrongRecipe, Path.of("wrong-world"), 21001L, "mc2p-visibility-wrong");
            throw new AssertionError("unsupported world version accepted");
        } catch (IllegalArgumentException expected) {}
        require(!Files.exists(Path.of("wrong-world")), "invalid recipe wrote an output");
        System.out.println("VISIBILITY_FIXTURE_NATIVE_OK");
    }

    private static NbtCompound readChunk(Path world, String kind, int x, int z) throws Exception {
        Path directory = world.resolve(kind);
        try (RegionFile region = new RegionFile(new StorageKey("test", World.OVERWORLD, kind),
                directory.resolve("r." + Math.floorDiv(x, 32) + "." + Math.floorDiv(z, 32) + ".mca"), directory, true);
             var input = region.getChunkInputStream(new ChunkPos(x, z))) {
            require(input != null, "missing region chunk");
            return NbtIo.readCompound(input);
        }
    }

    private static PalettedContainer<BlockState> decodeGroundSection(NbtCompound chunk) {
        for (NbtElement element : chunk.getList("sections", NbtElement.COMPOUND_TYPE)) {
            NbtCompound section = (NbtCompound) element;
            if (section.getByte("Y") == -4) return PalettedContainer.createPalettedContainerCodec(
                    Block.STATE_IDS, BlockState.CODEC, PalettedContainer.PaletteProvider.BLOCK_STATE,
                    Blocks.AIR.getDefaultState()).parse(NbtOps.INSTANCE, section.getCompound("block_states")).getOrThrow();
        }
        throw new AssertionError("ground section absent");
    }
    private static void require(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }
}
