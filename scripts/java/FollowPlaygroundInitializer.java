import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.block.*;
import net.minecraft.entity.boss.dragon.EnderDragonFight;
import net.minecraft.nbt.*;
import net.minecraft.util.math.ChunkPos;
import net.minecraft.world.World;
import net.minecraft.world.chunk.PalettedContainer;
import net.minecraft.world.storage.RegionFile;
import net.minecraft.world.storage.StorageKey;

/** Offline test initializer/verifier only. Never compiled into the actor or deployment mod. */
public final class FollowPlaygroundInitializer {
    private static net.minecraft.registry.RegistryWrapper.WrapperLookup wrappers;
    private static net.minecraft.item.Item[] HOTBAR;
    private static final String[] PLAYERS = {"MC2PFollower", "MC2PLeader"};
    public static void main(String[] args) throws Exception {
        if (args.length != 5) throw new IllegalArgumentException("build|verify recipe world seed scenario");
        SharedConstants.createGameVersion(); Bootstrap.initialize();
        HOTBAR = new net.minecraft.item.Item[]{net.minecraft.item.Items.STONE, net.minecraft.item.Items.COBBLESTONE,
            net.minecraft.item.Items.OAK_PLANKS, net.minecraft.item.Items.GLASS, net.minecraft.item.Items.WHITE_WOOL,
            net.minecraft.item.Items.TORCH, net.minecraft.item.Items.IRON_PICKAXE, net.minecraft.item.Items.IRON_AXE, net.minecraft.item.Items.IRON_SHOVEL};
        wrappers = net.minecraft.registry.BuiltinRegistries.createWrapperLookup();
        Path world = Path.of(args[2]).toAbsolutePath().normalize();
        long seed = Long.parseLong(args[3]);
        String scenario = args[4];
        if (seed < 21001 || seed > 21003 || !(scenario.equals("playground") || scenario.equals("playground_tracking") || scenario.equals("playground_search")))
            throw new IllegalArgumentException("undeclared follow fixture");
        if (args[0].equals("build")) build(Path.of(args[1]), world, seed, scenario);
        else if (!args[0].equals("verify")) throw new IllegalArgumentException("invalid mode");
        verify(world, seed, scenario);
        System.out.println("FOLLOW_PLAYGROUND_NATIVE_OK");
    }

    private static UUID identity(String name) {
        return UUID.nameUUIDFromBytes(("OfflinePlayer:"+name).getBytes(StandardCharsets.UTF_8));
    }
    private static NbtList doubles(double... values) {
        NbtList list = new NbtList(); for (double v: values) list.add(NbtDouble.of(v)); return list;
    }
    private static NbtCompound player(String name, long seed, String scenario) {
        boolean follower = name.equals(PLAYERS[0]);
        double x = .5 + seed - 21001 + (!follower && scenario.equals("playground_tracking") ? 3 : 0);
        NbtCompound data = new NbtCompound();
        data.putInt("DataVersion", 3953); data.putUuid("UUID", identity(name));
        data.put("Pos", doubles(x, -60, follower ? .5 : 6.5));
        data.put("Motion", doubles(0, 0, 0));
        NbtList rotation = new NbtList(); rotation.add(NbtFloat.of(0)); rotation.add(NbtFloat.of(32));
        data.put("Rotation", rotation); data.putString("Dimension", "minecraft:overworld");
        data.putBoolean("OnGround", true); data.putFloat("FallDistance", 0);
        data.putFloat("Health", 20); data.putInt("foodLevel", 20); data.putFloat("foodSaturationLevel", 5);
        data.putShort("Air", (short)300); data.putInt("playerGameType", follower ? 0 : 1); data.putInt("SelectedItemSlot", 0);
        NbtList inventory = new NbtList();
        if (!follower) for (int slot=0; slot<HOTBAR.length; slot++) {
            var stack = new net.minecraft.item.ItemStack(HOTBAR[slot], slot < 6 ? 64 : 1);
            NbtCompound entry = (NbtCompound) stack.encode(wrappers);
            entry.putByte("Slot", (byte)slot); inventory.add(entry);
        }
        data.put("Inventory", inventory); data.put("EnderItems", new NbtList());
        NbtCompound abilities = new NbtCompound();
        abilities.putBoolean("flying", false); abilities.putBoolean("mayfly", !follower);
        abilities.putBoolean("instabuild", !follower); abilities.putBoolean("invulnerable", !follower);
        abilities.putBoolean("mayBuild", true); abilities.putFloat("flySpeed", .05f); abilities.putFloat("walkSpeed", .1f);
        data.put("abilities", abilities);
        return data;
    }

    private static BlockState block(int x, int y, int z, long seed, String scenario) {
        int anchor = (int)seed-21001;
        if (y == -64) return Blocks.BEDROCK.getDefaultState();
        if (y == -63 || y == -62) return Blocks.DIRT.getDefaultState();
        if (y == -61) return Blocks.GRASS_BLOCK.getDefaultState();
        if (scenario.equals("playground_tracking") && x == anchor && z == 3 && y >= -60 && y <= -58)
            return Blocks.STONE.getDefaultState();
        if (scenario.equals("playground_search") && y >= -60 && y <= -58) {
            // Three-block open entry and one opaque corner. Test-only geometry;
            // the actor receives neither this layout nor the leader waypoints.
            boolean front = z == 9 && x >= anchor-3 && x <= anchor+5 && (x < anchor-1 || x > anchor+1);
            boolean corner = x == anchor+2 && z >= 9 && z <= 12;
            boolean sides = (x == anchor-3 || x == anchor+6) && z >= 9 && z <= 15;
            boolean back = z == 15 && x >= anchor-3 && x <= anchor+6;
            if (front || corner || sides || back) return Blocks.STONE.getDefaultState();
        }
        return Blocks.AIR.getDefaultState();
    }

    private static NbtCompound terrain(int cx, int cz, long seed, String scenario) {
        NbtCompound chunk = new NbtCompound();
        chunk.putInt("DataVersion", 3953); chunk.putInt("xPos", cx); chunk.putInt("zPos", cz); chunk.putInt("yPos", -4);
        chunk.putString("Status", "minecraft:full"); chunk.putLong("LastUpdate", 0); chunk.putLong("InhabitedTime", 0);
        chunk.putBoolean("isLightOn", false);
        NbtList sections = new NbtList();
        var codec = PalettedContainer.createPalettedContainerCodec(Block.STATE_IDS, BlockState.CODEC,
            PalettedContainer.PaletteProvider.BLOCK_STATE, Blocks.AIR.getDefaultState());
        for (int sy = -4; sy < 20; sy++) {
            var states = new PalettedContainer<BlockState>(Block.STATE_IDS, Blocks.AIR.getDefaultState(),
                PalettedContainer.PaletteProvider.BLOCK_STATE);
            if (sy == -4) for (int x=0;x<16;x++) for (int y=0;y<16;y++) for(int z=0;z<16;z++)
                states.set(x,y,z,block(cx*16+x,sy*16+y,cz*16+z,seed,scenario));
            NbtCompound section = new NbtCompound(); section.putByte("Y", (byte)sy);
            section.put("block_states", codec.encodeStart(NbtOps.INSTANCE,states).getOrThrow());
            NbtCompound biomes = new NbtCompound(); NbtList palette = new NbtList(); palette.add(NbtString.of("minecraft:plains"));
            biomes.put("palette",palette); section.put("biomes",biomes); sections.add(section);
        }
        chunk.put("sections",sections); chunk.put("Heightmaps",new NbtCompound());
        for (String key: new String[]{"block_ticks","fluid_ticks","PostProcessing","block_entities"}) chunk.put(key,new NbtList());
        NbtCompound structures = new NbtCompound(); structures.put("starts",new NbtCompound()); structures.put("References",new NbtCompound());
        chunk.put("structures",structures); return chunk;
    }

    private static RegionFile region(Path world, int cx, int cz) throws Exception {
        Path directory = world.resolve("region");
        return new RegionFile(new StorageKey("mc2p_follow_fixture",World.OVERWORLD,"chunk"),
            directory.resolve("r."+Math.floorDiv(cx,32)+"."+Math.floorDiv(cz,32)+".mca"),directory,true);
    }

    private static void build(Path recipe, Path world, long seed, String scenario) throws Exception {
        if (Files.exists(world,LinkOption.NOFOLLOW_LINKS)) throw new FileAlreadyExistsException(world.toString());
        if (!world.getParent().toRealPath().equals(world.getParent())) throw new IllegalArgumentException("indirect parent");
        NbtCompound root = StringNbtReader.parse(Files.readString(recipe));
        NbtCompound data = root.getCompound("Data");
        data.putString("LevelName","mc2p-playground-"+seed+"-"+scenario); data.remove("Player");
        data.putBoolean("allowCommands",false); data.putByte("Difficulty", (byte)0); data.putInt("GameType", 0);
        data.put("DragonFight",EnderDragonFight.Data.CODEC.encodeStart(NbtOps.INSTANCE,EnderDragonFight.Data.DEFAULT).getOrThrow());
        data.getCompound("WorldGenSettings").putLong("seed",seed);
        Files.createDirectory(world); Files.createDirectory(world.resolve("region")); Files.createDirectory(world.resolve("playerdata"));
        NbtIo.writeCompressed(root,world.resolve("level.dat"));
        for (String name: PLAYERS) NbtIo.writeCompressed(player(name,seed,scenario),world.resolve("playerdata/"+identity(name)+".dat"));
        for (int cx=-1;cx<=0;cx++) for(int cz=-1;cz<=0;cz++) try(var file=region(world,cx,cz)) {
            try(var output=file.getChunkOutputStream(new ChunkPos(cx,cz))) { NbtIo.writeCompound(terrain(cx,cz,seed,scenario),output); }
            file.sync();
        }
    }

    private static void verify(Path world, long seed, String scenario) throws Exception {
        NbtCompound data = NbtIo.readCompressed(world.resolve("level.dat"),NbtSizeTracker.of(4194304)).getCompound("Data");
        if (data.getInt("DataVersion")!=3953 || !data.getCompound("Version").getString("Name").equals("1.21")
                || !data.getString("LevelName").equals("mc2p-playground-"+seed+"-"+scenario)
                || data.getCompound("WorldGenSettings").getLong("seed")!=seed || data.getBoolean("allowCommands"))
            throw new IllegalArgumentException("world identity changed");
        for(String name:PLAYERS) {
            NbtCompound actual=NbtIo.readCompressed(world.resolve("playerdata/"+identity(name)+".dat"),NbtSizeTracker.of(4194304));
            if (name.equals("MC2PLeader")) {
                NbtList inventory = actual.getList("Inventory", NbtElement.COMPOUND_TYPE);
                if (inventory.size() != 9) throw new IllegalArgumentException("human hotbar coverage");
                for (int slot=0; slot<9; slot++) {
                    var stack = net.minecraft.item.ItemStack.fromNbt(wrappers, inventory.getCompound(slot)).orElseThrow();
                    if (inventory.getCompound(slot).getByte("Slot") != slot || !stack.isOf(HOTBAR[slot])
                            || stack.getCount() != (slot < 6 ? 64 : 1)) throw new IllegalArgumentException("human item codec roundtrip");
                }
            }
            if(!actual.equals(player(name,seed,scenario))) throw new IllegalArgumentException("player initializer changed");
        }
        var codec=PalettedContainer.createPalettedContainerCodec(Block.STATE_IDS,BlockState.CODEC,
            PalettedContainer.PaletteProvider.BLOCK_STATE,Blocks.AIR.getDefaultState());
        for(int cx=-1;cx<=0;cx++) for(int cz=-1;cz<=0;cz++) try(var file=region(world,cx,cz)) {
            NbtCompound chunk;
            try(var input=file.getChunkInputStream(new ChunkPos(cx,cz))) {
                if(input==null) throw new IllegalArgumentException("missing chunk");
                chunk=NbtIo.readCompound(input,NbtSizeTracker.of(8388608));
            }
            if(chunk.getInt("xPos")!=cx || chunk.getInt("zPos")!=cz || chunk.getInt("DataVersion")!=3953)
                throw new IllegalArgumentException("chunk identity changed");
            NbtList sections=chunk.getList("sections",NbtElement.COMPOUND_TYPE);
            if(sections.size()!=24) throw new IllegalArgumentException("section coverage changed");
            for(int i=0;i<sections.size();i++) {
                NbtCompound section=sections.getCompound(i); int sy=i-4;
                if(section.getByte("Y")!=sy) throw new IllegalArgumentException("section position changed");
                var states=codec.parse(NbtOps.INSTANCE,section.getCompound("block_states")).getOrThrow();
                for(int x=0;x<16;x++) for(int y=0;y<16;y++) for(int z=0;z<16;z++)
                    if(!states.get(x,y,z).equals(block(cx*16+x,sy*16+y,cz*16+z,seed,scenario)))
                        throw new IllegalArgumentException("decoded block differs from declared fixture");
            }
        }
    }
}
