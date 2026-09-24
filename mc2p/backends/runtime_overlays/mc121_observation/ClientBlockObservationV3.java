package com.mc2p.observation;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import net.minecraft.block.Block;
import net.minecraft.block.ShapeContext;
import net.minecraft.client.MinecraftClient;
import net.minecraft.entity.Entity;
import net.minecraft.registry.Registries;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.EntityHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.shape.VoxelShape;
import net.minecraft.world.BlockView;
import net.minecraft.world.RaycastContext;

/** Same-tick authorization and compact block fields. No world cache, images or surface permissions. */
public final class ClientBlockObservationV3 {
    public enum Source { FIRST_HIT_RAY, BODY_CONTACT, CURRENT_TARGET, SURFACE_DEPTH, AIR_QUERY }
    public interface SurfaceProvider { List<BlockPos> visible(MinecraftClient client, Vec3d eye, float yaw, float pitch); }
    private static SurfaceProvider surfaceProvider;
    public static boolean surfaceEnabled() { return "1".equals(System.getenv("MC2P_SURFACE_PERCEPTION")); }
    public static void installSurfaceProvider(SurfaceProvider provider) {
        if (!surfaceEnabled() || provider==null || surfaceProvider!=null) throw new IllegalStateException("surface provider installation refused");
        surfaceProvider=provider;
    }
    private ClientBlockObservationV3() {}

    public static void authorize(Map<BlockPos,EnumSet<Source>> table, BlockPos position, Source source) {
        if (table==null || position==null || source==null) throw new IllegalArgumentException("missing block authorization");
        table.computeIfAbsent(position.toImmutable(), key -> EnumSet.noneOf(Source.class)).add(source);
        if (table.size()>(surfaceEnabled()?26025:2456)) throw new IllegalStateException("block authorization budget exceeded");
    }
    public static List<BlockPos> orderedPositions(Map<BlockPos,EnumSet<Source>> table) {
        return table.keySet().stream().sorted(Comparator.comparingInt(BlockPos::getX)
            .thenComparingInt(BlockPos::getY).thenComparingInt(BlockPos::getZ)).toList();
    }

    public static Map<BlockPos,EnumSet<Source>> discover(BlockView world, Entity observer, Vec3d camera,
                                                         Box body, float yaw, float pitch) {
        var table = new HashMap<BlockPos,EnumSet<Source>>();
        for (int row=0; row<ClientObservationCollector.RAY_ROWS; row++) {
            for (int col=0; col<ClientObservationCollector.RAY_COLUMNS; col++) {
                Vec3d end = camera.add(ClientObservationCollector.rayDirection(yaw,pitch,row,col)
                    .multiply(ClientObservationCollector.BLOCK_MAX_DISTANCE));
                BlockHitResult hit = world.raycast(new RaycastContext(camera,end,
                    RaycastContext.ShapeType.OUTLINE,RaycastContext.FluidHandling.ANY,observer));
                if (hit.getType()==HitResult.Type.BLOCK) authorize(table,hit.getBlockPos(),Source.FIRST_HIT_RAY);
            }
        }
        discoverContacts(world,observer,body,table);
        return table;
    }

    private static void discoverContacts(BlockView world, Entity observer, Box body, Map<BlockPos,EnumSet<Source>> table) {
        // Body occupancy is direct proprioceptive knowledge. The expanded box separately
        // preserves the existing 0.05 contact tolerance for support, walls and fluids.
        Box expanded = body.expand(ClientObservationCollector.BODY_EXPANSION);
        ShapeContext context = ShapeContext.of(observer);
        BlockPos minimum = BlockPos.ofFloored(expanded.minX,expanded.minY,expanded.minZ);
        BlockPos maximum = BlockPos.ofFloored(expanded.maxX,expanded.maxY,expanded.maxZ);
        int contacts = 0;
        for (BlockPos mutable : BlockPos.iterate(minimum,maximum)) {
            BlockPos position = mutable.toImmutable();
            var state = world.getBlockState(position);
            boolean bodyOccupancy = new Box(position.getX(),position.getY(),position.getZ(),
                position.getX()+1.,position.getY()+1.,position.getZ()+1.).intersects(body);
            boolean collision = ClientObservationCollector.intersectsCollisionShape(expanded,position,state.getCollisionShape(world,position,context));
            var fluid = state.getFluidState();
            boolean fluidContact = !fluid.isEmpty() && new Box(position.getX(),position.getY(),position.getZ(),
                position.getX()+1.,position.getY()+fluid.getHeight(world,position),position.getZ()+1.).intersects(expanded);
            if (bodyOccupancy || collision || fluidContact) {
                if (++contacts>512) throw new IllegalStateException("contact budget exceeded");
                authorize(table,position,Source.BODY_CONTACT);
            }
        }
    }

    public static void discoverRequestedAir(BlockView world, Vec3d origin, ClientObservationRequestV3 request,
                                            Map<BlockPos,EnumSet<Source>> table) {
        if (world==null || origin==null || request==null || table==null)
            throw new IllegalArgumentException("missing air query input");
        for (ClientObservationRequestV3.Grid grid : request.airPositions()) {
            BlockPos position = new BlockPos(grid.x(),grid.y(),grid.z());
            double dx=position.getX()+.5-origin.x, dy=position.getY()+.5-origin.y, dz=position.getZ()+.5-origin.z;
            if (dx*dx+dy*dy+dz*dz>32.*32.) continue;
            if (world.getBlockState(position).isAir()) authorize(table,position,Source.AIR_QUERY);
        }
    }

    public static JsonArray readBlocks(BlockView world, ShapeContext context, Map<BlockPos,EnumSet<Source>> table) {
        boolean surface=surfaceEnabled();
        if (table.size()>(surface?26025:2456)) throw new IllegalStateException("block export budget exceeded");
        for (Source source : Source.values()) {
            int limit = source==Source.SURFACE_DEPTH ? (surface?25000:0) : source==Source.FIRST_HIT_RAY ? (surface?0:1431)
                : source==Source.BODY_CONTACT || source==Source.AIR_QUERY ? 512 : 1;
            if (table.values().stream().anyMatch(s -> s==null || s.isEmpty())
                    || table.values().stream().filter(s -> s.contains(source)).count()>limit)
                throw new IllegalStateException("invalid block source budget");
        }
        JsonArray blocks = new JsonArray();
        for (BlockPos position : orderedPositions(table)) {
            var state = world.getBlockState(position);
            JsonObject block = new JsonObject();
            block.add("position",grid(position));
            block.addProperty("block_id",Registries.BLOCK.getId(state.getBlock()).toString());
            block.add("collision",encodeCollision(state.getCollisionShape(world,position,context)));
            var fluid = state.getFluidState();
            if (fluid.isEmpty()) block.add("fluid_id",JsonNull.INSTANCE);
            else block.addProperty("fluid_id",Registries.FLUID.getId(fluid.getFluid()).toString());
            JsonArray sources = new JsonArray();
            table.get(position).stream().map(s -> s.name().toLowerCase(Locale.ROOT)).sorted().forEach(sources::add);
            block.add("sources",sources);
            blocks.add(block);
        }
        return blocks;
    }

    public static JsonObject encodeCollision(VoxelShape shape) {
        JsonObject value = new JsonObject();
        JsonArray boxes = new JsonArray();
        String kind = shape.isEmpty() ? "empty" : Block.isShapeFullCube(shape) ? "full_cube" : "boxes";
        if (kind.equals("boxes")) {
            var order = Comparator.comparingDouble((Box b)->b.minX).thenComparingDouble(b->b.minY)
                .thenComparingDouble(b->b.minZ).thenComparingDouble(b->b.maxX)
                .thenComparingDouble(b->b.maxY).thenComparingDouble(b->b.maxZ);
            for (Box box : shape.getBoundingBoxes().stream().distinct().sorted(order).toList()) {
                JsonArray part = new JsonArray();
                for (double n : new double[]{box.minX,box.minY,box.minZ,box.maxX,box.maxY,box.maxZ}) {
                    if (!Double.isFinite(n)) throw new IllegalStateException("nonfinite collision geometry");
                    part.add(n);
                }
                boxes.add(part);
            }
        }
        value.addProperty("kind",kind); value.add("boxes",boxes); value.add("reason",JsonNull.INSTANCE);
        return value;
    }

    private static JsonArray grid(BlockPos p) {
        JsonArray value = new JsonArray(); value.add(p.getX()); value.add(p.getY()); value.add(p.getZ()); return value;
    }

    public static JsonObject targeting(ClientObservationRequestV3 request, HitResult hit, Vec3d camera,
                                       ClientEntityIndex index, long tick) {
        if (!request.needsTargeting()) return ClientObservationCollector.missingGroup(tick,"client_perception_filtered","not_requested");
        if (hit==null) return ClientObservationCollector.missingGroup(tick,"client_perception_filtered","camera_or_target_missing");
        JsonObject value = new JsonObject();
        for (String key : new String[]{"block_position","entity_ref","face","hit_position","distance_blocks"}) value.add(key,JsonNull.INSTANCE);
        if (hit.getType()==HitResult.Type.MISS) value.addProperty("hit_kind","miss");
        else if (hit instanceof BlockHitResult blockHit) {
            value.addProperty("hit_kind","block"); value.add("block_position",grid(blockHit.getBlockPos()));
            value.addProperty("face",blockHit.getSide().asString());
        } else if (hit instanceof EntityHitResult entityHit) {
            var candidate = index.selected().stream().filter(c -> c.entity()==entityHit.getEntity()).findFirst();
            if (candidate.isEmpty()) {
                JsonObject unsupported = ClientObservationCollector.missingGroup(tick,"client_perception_filtered","target_entity_not_visible");
                unsupported.addProperty("status","unsupported"); return unsupported;
            }
            value.addProperty("hit_kind","entity"); value.addProperty("entity_ref",candidate.get().trackId());
        } else throw new IllegalStateException("unsupported target kind");
        if (hit.getType()!=HitResult.Type.MISS) {
            value.add("hit_position",ClientObservationCollector.vector(hit.getPos()));
            value.addProperty("distance_blocks",camera.distanceTo(hit.getPos()));
        }
        return ClientObservationCollector.validGroup(tick,"client_perception_filtered",value);
    }

    public static JsonObject collect(MinecraftClient client, ClientObservationRequestV3 request,
                                     ClientEntityIndex index, long generationId) {
        var player = client.player;
        var world = client.world;
        long tickBefore = world.getTime();
        Vec3d camera = player.getCameraPosVec(1.0f);
        float yaw = player.getYaw(), pitch = player.getPitch();
        Map<BlockPos,EnumSet<Source>> table;
        if (surfaceEnabled()) {
            if (surfaceProvider==null) throw new IllegalStateException("surface sensor selected but provider unavailable");
            table=new HashMap<>();
            for (BlockPos position : surfaceProvider.visible(client,camera,yaw,pitch)) authorize(table,position,Source.SURFACE_DEPTH);
            discoverContacts(world,player,player.getBoundingBox(),table);
        } else table = discover(world,player,camera,player.getBoundingBox(),yaw,pitch);
        discoverRequestedAir(world,player.getPos(),request,table);
        // Entity label legality already requires this query in V2. Reuse it for interaction.
        HitResult hit = ClientObservationCollector.currentTarget(client,player);
        var entities = ClientObservationCollector.collectVisibleEntities(client,player,camera,generationId,index,hit);
        if (request.needsTargeting() && hit instanceof BlockHitResult b && hit.getType()==HitResult.Type.BLOCK)
            authorize(table,b.getBlockPos(),Source.CURRENT_TARGET);
        ClientBlockParityDiagnostics.recordIfEnabled(ClientBlockParityDiagnostics.enabled(), generationId,
            request.fieldProfile(), tickBefore, table,
            () -> ClientObservationCollector.legacyFirstHitPositions(world,player,camera,yaw,pitch), world::getTime);
        JsonObject perception = ClientObservationCollector.perceptionMetadata();
        if (surfaceEnabled()) {
            if (ClientBlockParityDiagnostics.enabled()) throw new IllegalStateException("ray parity diagnostics cannot describe surface sensor");
            perception.addProperty("sensor_profile_revision",4);perception.addProperty("ray_columns",0);perception.addProperty("ray_rows",0);
        }
        perception.addProperty("knowledge_model","block_state_v1");
        perception.add("blocks",readBlocks(world,ShapeContext.of(player),table));
        perception.add("visible_entities",entities.values());
        perception.addProperty("entities_truncated",entities.truncatedCount()>0);
        perception.addProperty("truncated_entity_count",entities.truncatedCount());
        long tick = world.getTime();
        JsonObject result = new JsonObject();
        result.add("perception",ClientObservationCollector.validGroup(tick,"client_perception_filtered",perception));
        result.add("targeting",targeting(request,hit,camera,index,tick));
        return result;
    }
}
