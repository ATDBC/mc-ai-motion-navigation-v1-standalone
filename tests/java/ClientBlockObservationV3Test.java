import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mc2p.observation.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.block.ShapeContext;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.fluid.FluidState;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.EntityHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Direction;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.shape.VoxelShapes;
import net.minecraft.world.BlockView;
import net.minecraft.world.RaycastContext;

public class ClientBlockObservationV3Test {
    static void require(boolean value, String reason) { if (!value) throw new AssertionError(reason); }
    static void rejected(Runnable call) {
        try { call.run(); } catch (IllegalArgumentException | IllegalStateException expected) { return; }
        throw new AssertionError("invalid request was accepted");
    }
    public static void main(String[] args) throws Exception {
        SharedConstants.createGameVersion(); Bootstrap.initialize();
        var nav = ClientObservationRequestV3.navigation();
        var interaction = new ClientObservationRequestV3("interaction_v1");
        require(!nav.needsTargeting() && interaction.needsTargeting(), "profile selection");
        require(ClientObservationRequestV3.decode("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"navigation_v1\"}"
            .getBytes(StandardCharsets.UTF_8)).equals(nav), "request decode");
        for (String raw : new String[]{"{}", "[]", "null",
            "{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":true}",
            "{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"all\"}",
            "{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"navigation_v1\",\"field_profile\":\"navigation_v1\"}"})
            rejected(() -> ClientObservationRequestV3.decode(raw.getBytes(StandardCharsets.UTF_8)));
        rejected(() -> ClientObservationRequestV3.decode(new byte[]{(byte)255}));
        rejected(() -> ClientObservationRequestV3.decode(new byte[16385]));
        rejected(() -> new ClientObservationRequestV3(null));

        var table = new HashMap<BlockPos, EnumSet<ClientBlockObservationV3.Source>>();
        var p = new BlockPos(0, 63, 0);
        for (int i=0; i<318; i++) ClientBlockObservationV3.authorize(table, p, ClientBlockObservationV3.Source.SURFACE_DEPTH);
        ClientBlockObservationV3.authorize(table, p, ClientBlockObservationV3.Source.BODY_CONTACT);
        require(table.size()==1 && table.get(p).size()==2, "duplicate block knowledge");
        var world = new TestBlocks(); world.states.put(p, Blocks.STONE.getDefaultState());
        world.allowed = Set.of(p);
        JsonArray blocks = ClientBlockObservationV3.readBlocks(world, ShapeContext.absent(), table);
        require(world.reads==1, "repeated export or hidden block read");
        JsonObject block = blocks.get(0).getAsJsonObject();
        require(block.keySet().equals(Set.of("position","block_id","collision","fluid_id","sources")), "non-minimal fields");
        require(block.getAsJsonObject("collision").get("kind").getAsString().equals("full_cube"), "wrong full shape");
        var one = new HashMap<BlockPos, EnumSet<ClientBlockObservationV3.Source>>();
        ClientBlockObservationV3.authorize(one,p,ClientBlockObservationV3.Source.SURFACE_DEPTH);
        var repeated = ClientBlockObservationV3.readBlocks(world, ShapeContext.absent(), one);
        require(repeated.get(0).getAsJsonObject().get("collision").equals(block.get("collision")), "surface source count affects shape");
        var mutable = new BlockPos.Mutable(2,63,0);
        ClientBlockObservationV3.authorize(one,mutable,ClientBlockObservationV3.Source.SURFACE_DEPTH);
        mutable.set(99,99,99);
        require(ClientBlockObservationV3.orderedPositions(one).equals(List.of(p,new BlockPos(2,63,0))), "mutable coordinate or unstable order");

        var shape = ClientBlockObservationV3.encodeCollision(VoxelShapes.cuboid(-.25,0,0,1.25,1.5,1));
        require(shape.getAsJsonArray("boxes").get(0).getAsJsonArray().get(0).getAsDouble()==-.25, "clipped shape");
        require(shape.getAsJsonArray("boxes").get(0).getAsJsonArray().get(4).getAsDouble()==1.5, "clipped tall shape");
        require(ClientBlockObservationV3.encodeCollision(VoxelShapes.empty()).get("kind").getAsString().equals("empty"), "empty fabricated");

        var airTable = new HashMap<BlockPos, EnumSet<ClientBlockObservationV3.Source>>();
        var airPos = new BlockPos(1,64,1);
        var solidPos = new BlockPos(2,64,1);
        world.states.put(solidPos,Blocks.STONE.getDefaultState());
        world.allowed=Set.of(airPos,solidPos);
        var airRequest = new ClientObservationRequestV3("navigation_v1",List.of(
            new ClientObservationRequestV3.Grid(1,64,1),new ClientObservationRequestV3.Grid(2,64,1)));
        ClientBlockObservationV3.discoverRequestedAir(world,new Vec3d(.5,64,.5),airRequest,airTable);
        require(airTable.keySet().equals(Set.of(airPos))
            && airTable.get(airPos).equals(EnumSet.of(ClientBlockObservationV3.Source.AIR_QUERY)),
            "air query leaked a non-air block or omitted air");
        world.allowed=Set.of(airPos);
        var airBlocks=ClientBlockObservationV3.readBlocks(world,ShapeContext.absent(),airTable);
        require(airBlocks.size()==1 && airBlocks.get(0).getAsJsonObject().get("block_id").getAsString().equals("minecraft:air"),
            "air query did not export its positive result");

        world = new TestBlocks();
        world.states.put(p, Blocks.STONE.getDefaultState());
        world.states.put(new BlockPos(0,62,0), Blocks.DIAMOND_ORE.getDefaultState());
        var observer = new ArmorStandEntity(EntityType.ARMOR_STAND,new DetachedTestWorld());
        var seen = ClientBlockObservationV3.discoverBodyContacts(
            world,observer,new Box(.2,64,.2,.8,65.8,.8));
        require(seen.containsKey(p) && seen.get(p).contains(ClientBlockObservationV3.Source.BODY_CONTACT), "underfoot permission omitted");
        require(seen.containsKey(new BlockPos(0,64,0))
                && seen.get(new BlockPos(0,64,0)).contains(ClientBlockObservationV3.Source.BODY_CONTACT)
                && seen.containsKey(new BlockPos(0,65,0))
                && seen.get(new BlockPos(0,65,0)).contains(ClientBlockObservationV3.Source.BODY_CONTACT),
            "air occupied by the actor body was not known directly");
        var bodyAirTable = new HashMap<BlockPos,EnumSet<ClientBlockObservationV3.Source>>();
        for (BlockPos position : List.of(new BlockPos(0,64,0),new BlockPos(0,65,0)))
            bodyAirTable.put(position,seen.get(position));
        world.allowed=bodyAirTable.keySet();
        var bodyAirBlocks=ClientBlockObservationV3.readBlocks(world,ShapeContext.of(observer),bodyAirTable);
        require(bodyAirBlocks.size()==2 && bodyAirBlocks.asList().stream().allMatch(value ->
                value.getAsJsonObject().get("block_id").getAsString().equals("minecraft:air")),
            "body occupancy did not export air facts");
        world.allowed=null;
        require(!seen.containsKey(new BlockPos(0,62,0)), "underground permission leaked");

        var feetWorld = new TestBlocks();
        for (BlockPos support : List.of(new BlockPos(0,63,0),new BlockPos(1,63,0),new BlockPos(0,63,1),new BlockPos(1,63,1)))
            feetWorld.states.put(support,Blocks.STONE.getDefaultState());
        feetWorld.states.put(new BlockPos(0,62,0),Blocks.DIAMOND_ORE.getDefaultState());
        var feet = ClientBlockObservationV3.discoverBodyContacts(feetWorld,observer,
            new Box(.7,64,.7,1.3,65.8,1.3));
        var bodyAndSupport = new HashSet<BlockPos>();
        for (int x=0; x<=1; x++) for (int z=0; z<=1; z++) {
            bodyAndSupport.add(new BlockPos(x,63,z));
            bodyAndSupport.add(new BlockPos(x,64,z));
            bodyAndSupport.add(new BlockPos(x,65,z));
        }
        require(feet.keySet().equals(bodyAndSupport)
                && feet.values().stream().allMatch(s->s.equals(EnumSet.of(ClientBlockObservationV3.Source.BODY_CONTACT))),
            "body occupancy or straddled support permission was incomplete, view-dependent, or leaked underground");

        // Contact authorization must use the same entity-sensitive geometry as exported collision.
        var contextual = new TestBlocks();
        var actor = new ArmorStandEntity(EntityType.ARMOR_STAND,new DetachedTestWorld());
        actor.setPosition(.5,64,.5);
        contextual.states.put(p,Blocks.SCAFFOLDING.getDefaultState());
        var support = ClientBlockObservationV3.discoverBodyContacts(contextual,actor,actor.getBoundingBox());
        require(support.containsKey(p), "standing scaffold support missing");
        actor.setSneaking(true);
        require(contextual.states.get(p).getCollisionShape(contextual,p,ShapeContext.of(actor)).isEmpty(),
            "fixture must descend through scaffold");
        var descending = ClientBlockObservationV3.discoverBodyContacts(contextual,actor,actor.getBoundingBox());
        require(!descending.containsKey(p), "context-free scaffold shape granted false contact");
        actor.setSneaking(false);
        contextual.states.put(p,Blocks.POWDER_SNOW.getDefaultState());
        var bare = ClientBlockObservationV3.discoverBodyContacts(contextual,actor,actor.getBoundingBox());
        require(!bare.containsKey(p), "bare feet granted solid snow contact");
        actor.equipStack(net.minecraft.entity.EquipmentSlot.FEET,new net.minecraft.item.ItemStack(net.minecraft.item.Items.LEATHER_BOOTS));
        var booted = ClientBlockObservationV3.discoverBodyContacts(contextual,actor,actor.getBoundingBox());
        require(booted.containsKey(p), "leather-boot snow support omitted by absent context");

        var index = new ClientEntityIndex();
        HitResult poison = new HitResult(Vec3d.ZERO) { public Type getType() { throw new AssertionError("nav read targeting"); } };
        require(ClientBlockObservationV3.targeting(nav,poison,Vec3d.ZERO,index,100).get("reason_code").getAsString().equals("not_requested"), "nav old target retained");
        var targetPos = new BlockPos(0,64,3);
        var hit = new BlockHitResult(new Vec3d(.5,64.5,3),Direction.NORTH,targetPos,false);
        var targeting = ClientBlockObservationV3.targeting(interaction,hit,new Vec3d(.5,64.5,.5),index,100);
        require(targeting.getAsJsonObject("value").get("face").getAsString().equals("north"), "wrong targeting face");
        var targetTable = new HashMap<BlockPos, EnumSet<ClientBlockObservationV3.Source>>();
        ClientBlockObservationV3.authorize(targetTable,targetPos,ClientBlockObservationV3.Source.CURRENT_TARGET);
        world.allowed = Set.of(targetPos);
        var targetBlocks = ClientBlockObservationV3.readBlocks(world,ShapeContext.absent(),targetTable);
        var miss = BlockHitResult.createMissed(Vec3d.ZERO,Direction.NORTH,BlockPos.ORIGIN);
        require(ClientBlockObservationV3.targeting(interaction,miss,Vec3d.ZERO,index,100).getAsJsonObject("value").get("hit_kind").getAsString().equals("miss"), "miss confused with missing");
        require(ClientBlockObservationV3.targeting(interaction,null,Vec3d.ZERO,index,100).get("status").getAsString().equals("missing"), "missing target invented");
        var entityHit = new EntityHitResult(observer,new Vec3d(1,64,1));
        index.beginFrame(world,1,List.of(observer));
        require(ClientBlockObservationV3.targeting(interaction,entityHit,Vec3d.ZERO,index,100).get("status").getAsString().equals("unsupported"), "hidden entity reference leaked");
        index.offer(observer,new Vec3d(1,0,1),"minecraft:armor_stand");
        require(ClientBlockObservationV3.targeting(interaction,entityHit,Vec3d.ZERO,index,100).getAsJsonObject("value").get("entity_ref").getAsString().equals(index.selected().get(0).trackId()), "wrong local reference");
        JsonObject result = new JsonObject(); result.add("blocks",blocks); result.add("air_blocks",airBlocks);
        result.add("body_air_blocks",bodyAirBlocks);
        result.add("target_blocks",targetBlocks); result.add("targeting",targeting);
        result.add("missing_frame",verifyMissingClient(nav,interaction));
        Files.write(Path.of(args[0]),ClientObservationJson.encode(result));
        System.out.println("CLIENT_BLOCK_OBSERVATION_V3_OK");
    }
    private static JsonObject verifyMissingClient(ClientObservationRequestV3 nav, ClientObservationRequestV3 interaction) throws Exception {
        rejected(() -> ClientObservationCollector.collectV3(null,0,nav));
        var unsafeField = sun.misc.Unsafe.class.getDeclaredField("theUnsafe"); unsafeField.setAccessible(true);
        var unsafe = (sun.misc.Unsafe)unsafeField.get(null);
        var client = (net.minecraft.client.MinecraftClient)unsafe.allocateInstance(net.minecraft.client.MinecraftClient.class);
        rejected(() -> ClientObservationCollector.collectV3(client,0,nav));
        // No constructor, window, resources or world. Only exercise the missing-state branch.
        var method = ClientObservationCollector.class.getDeclaredMethod("collectState",net.minecraft.client.MinecraftClient.class,long.class,ClientObservationRequestV3.class);
        method.setAccessible(true);
        JsonObject missing = (JsonObject)method.invoke(null,client,0L,nav);
        require(missing.get("schema_version").getAsString().equals("mc2p.client_observation.v3"),"missing state relabelled V2");
        require(missing.getAsJsonObject("perception").get("value").isJsonNull(),"missing world queried");
        require(missing.getAsJsonObject("targeting").get("reason_code").getAsString().equals("not_requested"),"nav missing state retained target");
        JsonObject requested = (JsonObject)method.invoke(null,client,1L,interaction);
        require(requested.getAsJsonObject("targeting").get("reason_code").getAsString().equals("player_or_world_missing"),"requested query pretended miss");
        var thread = net.minecraft.client.MinecraftClient.class.getDeclaredField("thread"); thread.setAccessible(true);
        thread.set(client,Thread.currentThread());
        return com.google.gson.JsonParser.parseString(new String(ClientObservationCollector.collectV3(client,2L,nav),StandardCharsets.UTF_8)).getAsJsonObject();
    }
    static class TestBlocks implements BlockView {
        final Map<BlockPos,BlockState> states = new HashMap<>();
        Set<BlockPos> allowed;
        int reads;
        int rays;
        public BlockHitResult raycast(RaycastContext context) { rays++; return BlockView.super.raycast(context); }
        public BlockState getBlockState(BlockPos position) {
            if (allowed!=null && !allowed.contains(position)) throw new AssertionError("unauthorized export "+position);
            reads++; return states.getOrDefault(position,Blocks.AIR.getDefaultState());
        }
        public FluidState getFluidState(BlockPos position) { return states.getOrDefault(position,Blocks.AIR.getDefaultState()).getFluidState(); }
        public BlockEntity getBlockEntity(BlockPos position) { throw new AssertionError("block entity content read"); }
        public int getHeight() { return 384; }
        public int getBottomY() { return -64; }
    }
}
