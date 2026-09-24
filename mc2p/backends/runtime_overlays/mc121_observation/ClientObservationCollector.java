package com.mc2p.observation;

import com.mc2p.actions.ClientBehaviorInput;
import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ingame.HandledScreen;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.component.DataComponentTypes;
import net.minecraft.block.Block;
import net.minecraft.block.BlockState;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.effect.StatusEffectInstance;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.inventory.CraftingInventory;
import net.minecraft.inventory.CraftingResultInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.state.property.Property;
import net.minecraft.screen.AbstractFurnaceScreenHandler;
import net.minecraft.screen.AnvilScreenHandler;
import net.minecraft.screen.BeaconScreenHandler;
import net.minecraft.screen.CraftingScreenHandler;
import net.minecraft.screen.EnchantmentScreenHandler;
import net.minecraft.screen.GenericContainerScreenHandler;
import net.minecraft.screen.HopperScreenHandler;
import net.minecraft.screen.HorseScreenHandler;
import net.minecraft.screen.MerchantScreenHandler;
import net.minecraft.screen.PlayerScreenHandler;
import net.minecraft.screen.ScreenHandler;
import net.minecraft.screen.ShulkerBoxScreenHandler;
import net.minecraft.screen.slot.CrafterOutputSlot;
import net.minecraft.screen.slot.CraftingResultSlot;
import net.minecraft.screen.slot.FurnaceOutputSlot;
import net.minecraft.screen.slot.Slot;
import net.minecraft.screen.slot.TradeOutputSlot;
import net.minecraft.text.Text;
import net.minecraft.util.Hand;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.EntityHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.MathHelper;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.shape.VoxelShape;
import net.minecraft.world.LightType;
import net.minecraft.world.BlockView;
import net.minecraft.world.RaycastContext;

/** Reads only state available to a normal Minecraft client at one client-tick boundary. */
public final class ClientObservationCollector {
    private static final String SCHEMA = "mc2p.client_observation.v2";
    static final int RAY_COLUMNS = 159;
    static final int RAY_ROWS = 9;
    private static final int SENSOR_PROFILE_REVISION = 3;
    private static final double HORIZONTAL_FOV = 120.0;
    private static final double VERTICAL_FOV = 120.0;
    static final double BLOCK_MAX_DISTANCE = 16.0;
    static final double BODY_EXPANSION = 0.05;
    private static final double ENTITY_MAX_DISTANCE = 32.0;
    private static final double ENTITY_OCCLUSION_EPSILON = 0.05;
    private static final int ENTITY_VISIBILITY_SAMPLE_COUNT = 5;
    private static final ClientEntityIndex ENTITY_INDEX = new ClientEntityIndex();
    private static final ClientGuiSession GUI_SESSION = new ClientGuiSession();
    private static Object guiWorld;
    private static final ClientSampleClock SAMPLE_CLOCK = new ClientSampleClock();

    private ClientObservationCollector() {}

    public static ClientSampleClock.DiagnosticPoint diagnosticSampleClock() {
        return SAMPLE_CLOCK.diagnosticPoint();
    }

    /** Resolve only an identity already assigned by the latest formal entity frame. */
    public static String currentTrackId(Entity entity) {
        return entity == null ? null : ENTITY_INDEX.trackId(entity);
    }

    public static byte[] collect(MinecraftClient client, long generationId) {
        if (client == null || generationId < 0L) {
            throw new IllegalArgumentException("invalid client observation request");
        }
        if (!client.isOnThread()) throw new IllegalStateException("observation requires client thread");
        return ClientObservationJson.encode(SAMPLE_CLOCK.sample(() -> collectState(client, generationId)));
    }

    private static JsonObject collectState(MinecraftClient client, long generationId) {
        return collectState(client, generationId, null);
    }

    public static byte[] collectV3(MinecraftClient client, long generationId, ClientObservationRequestV3 request) {
        if (client==null || request==null || generationId<0L) throw new IllegalArgumentException("invalid V3 observation request");
        if (!client.isOnThread()) throw new IllegalStateException("observation requires client thread");
        return ClientObservationJson.encode(SAMPLE_CLOCK.sample(() -> collectState(client,generationId,request)));
    }

    private static JsonObject collectState(MinecraftClient client, long generationId, ClientObservationRequestV3 request) {
        if (generationId == 0L || guiWorld != client.world) {
            GUI_SESSION.reset();
            guiWorld = client.world;
        }
        ClientPlayerEntity player = client.player;
        long worldTick = client.world == null ? 0L : client.world.getTime();
        JsonObject root = ClientObservationJson.object();
        root.addProperty("schema_version", request==null ? SCHEMA : "mc2p.client_observation.v3");
        if (request!=null) root.addProperty("field_profile",request.fieldProfile());
        root.addProperty("generation_id", generationId);
        root.addProperty("sample_world_tick", worldTick);
        if (player == null || client.world == null) {
            ENTITY_INDEX.clear();
            root.add("self_state", missingGroup(worldTick, "client_player", "player_or_world_missing"));
            root.add("inventory", missingGroup(worldTick, "client_inventory", "player_or_world_missing"));
            root.add("gui", missingGroup(worldTick, "client_screen_handler", "player_or_world_missing"));
            root.add("perception", missingGroup(worldTick, "client_perception_filtered", "player_or_world_missing"));
            if (request!=null) root.add("targeting",missingGroup(worldTick,"client_perception_filtered",
                request.needsTargeting() ? "player_or_world_missing" : "not_requested"));
            if (request!=null) root.add("tracked_entity",missingGroup(worldTick,"client_registered_entity",
                request.entityTrackId()==null ? "not_requested" : "player_or_world_missing"));
            return root;
        }
        root.add("self_state", validGroup(worldTick, "client_player", collectSelf(client, player)));
        root.add("inventory", validGroup(worldTick, "client_inventory", collectInventory(player)));
        root.add("gui", collectGuiGroup(client, player, worldTick));
        if (request!=null) {
            JsonObject groups = ClientBlockObservationV3.collect(client,request,ENTITY_INDEX,generationId);
            root.add("perception",groups.get("perception"));
            root.add("targeting",groups.get("targeting"));
            root.add("tracked_entity",trackedEntityGroup(worldTick,request,ENTITY_INDEX,
                    player.getPos(),player.getVelocity(),player.getYaw()));
        } else root.add(
                "perception",
                validGroup(
                        worldTick,
                        "client_perception_filtered",
                        collectPerception(client, player, generationId)));
        return root;
    }

    static JsonObject trackedEntityGroup(long tick, ClientObservationRequestV3 request,
            ClientEntityIndex index, Vec3d observerPosition, Vec3d observerVelocity, float observerYaw) {
        String trackId = request.entityTrackId();
        if (trackId == null) return missingGroup(tick,"client_registered_entity","not_requested");
        Entity entity = index.resolveLoaded(trackId);
        if (entity == null) return missingGroup(tick,"client_registered_entity","entity_unavailable");
        if (!(entity instanceof LivingEntity living))
            return missingGroup(tick,"client_registered_entity","not_living_entity");
        Box box = entity.getBoundingBox();
        JsonObject value = ClientObservationJson.object();
        value.addProperty("track_id",trackId);
        value.addProperty("entity_type",Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        value.add("relative_position",vector(entity.getPos().subtract(observerPosition)));
        value.add("relative_velocity",vector(entity.getVelocity().subtract(observerVelocity)));
        value.addProperty("relative_yaw_degrees",MathHelper.wrapDegrees(entity.getYaw()-observerYaw));
        value.addProperty("pitch_degrees",entity.getPitch());
        value.add("bounding_box_size",vector(new Vec3d(box.getLengthX(),box.getLengthY(),box.getLengthZ())));
        value.addProperty("pose",entity.getPose().name().toLowerCase(Locale.ROOT));
        value.addProperty("is_on_ground",entity.isOnGround());
        value.addProperty("is_loaded",true);
        value.addProperty("is_dead",living.isDead());
        value.addProperty("health_points",living.getHealth());
        value.addProperty("max_health_points",living.getMaxHealth());
        return validGroup(tick,"client_registered_entity",value);
    }

    static JsonObject validGroup(long tick, String source, JsonObject value) {
        JsonObject group = ClientObservationJson.object();
        group.addProperty("status", "valid");
        group.addProperty("sample_world_tick", tick);
        group.addProperty("source_kind", source);
        group.add("reason_code", JsonNull.INSTANCE);
        group.add("value", value);
        return group;
    }

    static JsonObject missingGroup(long tick, String source, String reason) {
        JsonObject group = ClientObservationJson.object();
        group.addProperty("status", "missing");
        group.addProperty("sample_world_tick", tick);
        group.addProperty("source_kind", source);
        group.addProperty("reason_code", reason);
        group.add("value", JsonNull.INSTANCE);
        return group;
    }

    private static JsonObject collectSelf(MinecraftClient client, ClientPlayerEntity player) {
        JsonObject value = ClientObservationJson.object();
        value.add("position", vector(player.getPos()));
        value.add("velocity", vector(player.getVelocity()));
        value.addProperty("yaw_degrees", player.getYaw());
        value.addProperty("pitch_degrees", player.getPitch());
        value.addProperty("head_yaw_degrees", player.getHeadYaw());
        value.addProperty("body_yaw_degrees", player.getBodyYaw());
        value.addProperty("is_dead", player.isDead());
        value.addProperty("is_on_ground", player.isOnGround());
        value.addProperty("horizontal_collision", player.horizontalCollision);
        value.addProperty("vertical_collision", player.verticalCollision);
        value.addProperty("pose", player.getPose().name().toLowerCase(Locale.ROOT));
        value.addProperty("is_sprinting", player.isSprinting());
        value.addProperty("is_sneaking", player.isSneaking());
        value.addProperty("is_swimming", player.isSwimming());
        value.addProperty("is_submerged_in_water", player.isSubmergedInWater());
        value.addProperty("is_climbing", player.isClimbing());
        value.addProperty("is_fall_flying", player.isFallFlying());
        value.addProperty("is_burning", player.isOnFire());
        value.addProperty("fall_distance_blocks", player.fallDistance);
        value.addProperty("air_ticks", player.getAir());
        value.addProperty("max_air_ticks", player.getMaxAir());
        value.addProperty("health_points", player.getHealth());
        value.addProperty("max_health_points", player.getMaxHealth());
        value.addProperty("absorption_points", player.getAbsorptionAmount());
        value.addProperty("armor_points", player.getArmor());
        value.addProperty("food_points", player.getHungerManager().getFoodLevel());
        value.addProperty("saturation_points", player.getHungerManager().getSaturationLevel());
        value.addProperty("experience_level", player.experienceLevel);
        value.addProperty("experience_progress", player.experienceProgress);
        value.addProperty("total_experience", player.totalExperience);
        value.addProperty("attack_cooldown", player.getAttackCooldownProgress(0.0f));
        ClientBehaviorInput.Sample inputSample = player.input instanceof ClientBehaviorInput input
                ? input.lastSample()
                : null;
        if (inputSample == null) {
            value.add("hurt_animation_ticks", JsonNull.INSTANCE);
            value.add("movement_tick_id", JsonNull.INSTANCE);
        } else {
            value.addProperty("hurt_animation_ticks", player.hurtTime);
            value.addProperty("movement_tick_id", inputSample.movementTickId());
        }
        if (player.isUsingItem()) {
            value.addProperty(
                    "active_hand",
                    player.getActiveHand() == Hand.MAIN_HAND ? "main_hand" : "off_hand");
        } else {
            value.add("active_hand", JsonNull.INSTANCE);
        }
        value.addProperty("is_using_item", player.isUsingItem());
        value.addProperty("item_use_ticks_remaining", player.getItemUseTimeLeft());
        value.add("status_effects", statusEffects(player));
        String gameMode = client.interactionManager == null
                ? "survival"
                : client.interactionManager.getCurrentGameMode().asString();
        value.addProperty("game_mode", gameMode);
        value.addProperty("is_flying", player.getAbilities().flying);
        value.addProperty("allow_flying", player.getAbilities().allowFlying);
        return value;
    }

    private static JsonArray statusEffects(ClientPlayerEntity player) {
        List<StatusEffectInstance> effects = new ArrayList<>(player.getStatusEffects());
        effects.sort(Comparator.comparing(ClientObservationCollector::statusEffectId));
        JsonArray result = ClientObservationJson.array();
        for (StatusEffectInstance effect : effects) {
            JsonObject item = ClientObservationJson.object();
            item.addProperty("effect_id", statusEffectId(effect));
            item.addProperty("amplifier", effect.getAmplifier());
            item.addProperty("duration_ticks", Math.max(0, effect.getDuration()));
            item.addProperty("ambient", effect.isAmbient());
            item.addProperty("show_particles", effect.shouldShowParticles());
            item.addProperty("show_icon", effect.shouldShowIcon());
            result.add(item);
        }
        return result;
    }

    private static String statusEffectId(StatusEffectInstance effect) {
        return Registries.STATUS_EFFECT.getId(effect.getEffectType().value()).toString();
    }

    private static JsonObject collectInventory(ClientPlayerEntity player) {
        PlayerInventory inventory = player.getInventory();
        JsonObject value = ClientObservationJson.object();
        JsonArray main = ClientObservationJson.array();
        for (int index = 0; index < PlayerInventory.MAIN_SIZE; index++) {
            main.add(item(inventory.main.get(index)));
        }
        value.add("main", main);
        value.addProperty("selected_hotbar_slot", inventory.selectedSlot);
        value.add("head", item(player.getEquippedStack(EquipmentSlot.HEAD)));
        value.add("chest", item(player.getEquippedStack(EquipmentSlot.CHEST)));
        value.add("legs", item(player.getEquippedStack(EquipmentSlot.LEGS)));
        value.add("feet", item(player.getEquippedStack(EquipmentSlot.FEET)));
        value.add("offhand", item(player.getEquippedStack(EquipmentSlot.OFFHAND)));
        value.add("main_hand", item(inventory.main.get(inventory.selectedSlot)));
        return value;
    }

    private static JsonObject item(ItemStack stack) {
        JsonObject value = ClientObservationJson.object();
        if (stack == null || stack.isEmpty()) {
            value.addProperty("empty", true);
            value.add("item_id", JsonNull.INSTANCE);
            value.addProperty("count", 0);
            value.addProperty("damage", 0);
            value.addProperty("max_damage", 0);
            value.addProperty("damageable", false);
            value.add("custom_name", JsonNull.INSTANCE);
            return value;
        }
        value.addProperty("empty", false);
        value.addProperty("item_id", Registries.ITEM.getId(stack.getItem()).toString());
        value.addProperty("count", stack.getCount());
        value.addProperty("damage", stack.getDamage());
        value.addProperty("max_damage", stack.getMaxDamage());
        value.addProperty("damageable", stack.isDamageable());
        Text customName = stack.get(DataComponentTypes.CUSTOM_NAME);
        if (customName == null) value.add("custom_name", JsonNull.INSTANCE);
        else value.addProperty("custom_name", customName.getString());
        return value;
    }

    private static JsonObject closedGui() {
        JsonObject value = ClientObservationJson.object();
        value.addProperty("open", false);
        value.add("gui_session_id", JsonNull.INSTANCE);
        value.addProperty("screen_kind", "closed");
        value.add("handler_type", JsonNull.INSTANCE);
        value.add("sync_id", JsonNull.INSTANCE);
        value.add("revision", JsonNull.INSTANCE);
        value.add("title", JsonNull.INSTANCE);
        value.add("slots", ClientObservationJson.array());
        value.add("cursor_stack", item(ItemStack.EMPTY));
        value.add("properties", ClientObservationJson.array());
        value.addProperty("properties_status", "valid");
        value.add("properties_reason_code", JsonNull.INSTANCE);
        value.add("focused_slot_id", JsonNull.INSTANCE);
        return value;
    }

    private static JsonObject collectGuiGroup(
            MinecraftClient client,
            ClientPlayerEntity player,
            long worldTick) {
        String session = currentGuiSessionId(client);
        if (!(client.currentScreen instanceof HandledScreen<?> handledScreen)) {
            return validGroup(worldTick, "client_screen_handler", closedGui());
        }
        ScreenHandler handler = handledScreen.getScreenHandler();
        if (handler != player.currentScreenHandler) {
            return missingGroup(worldTick, "client_screen_handler", "screen_handler_mismatch");
        }
        JsonObject value = collectGui(handledScreen, handler, player);
        value.addProperty("gui_session_id", session);
        return validGroup(worldTick, "client_screen_handler", value);
    }

    public static String currentGuiSessionId(MinecraftClient client) {
        if (!client.isOnThread()) throw new IllegalStateException("GUI reference queried off client thread");
        if (guiWorld != client.world) {
            GUI_SESSION.reset();
            guiWorld = client.world;
        }
        if (client.player == null || !(client.currentScreen instanceof HandledScreen<?> screen)
                || screen.getScreenHandler() != client.player.currentScreenHandler) {
            return GUI_SESSION.observe(null, null);
        }
        return GUI_SESSION.observe(screen, screen.getScreenHandler());
    }

    private static JsonObject collectGui(
            HandledScreen<?> screen,
            ScreenHandler handler,
            ClientPlayerEntity player) {
        JsonObject value = ClientObservationJson.object();
        value.addProperty("open", true);
        value.addProperty("screen_kind", screenKind(handler));
        value.addProperty("handler_type", handlerType(handler));
        value.addProperty("sync_id", handler.syncId);
        value.addProperty("revision", handler.getRevision());
        value.addProperty("title", screen.getTitle().getString());
        JsonArray slots = ClientObservationJson.array();
        for (Slot slot : handler.slots) {
            JsonObject slotValue = ClientObservationJson.object();
            slotValue.addProperty("slot_id", slot.id);
            slotValue.addProperty("x", slot.x);
            slotValue.addProperty("y", slot.y);
            slotValue.addProperty("source_kind", slotSourceKind(slot, player));
            slotValue.addProperty("source_index", slot.getIndex());
            slotValue.add("item", item(slot.getStack()));
            slotValue.addProperty("enabled", slot.isEnabled());
            slotValue.addProperty("can_take", slot.canTakeItems(player));
            slots.add(slotValue);
        }
        value.add("slots", slots);
        value.add("cursor_stack", item(handler.getCursorStack()));
        var properties = ((ClientGuiPropertyAccess) handler).mc2p$properties();
        var publicProperties = ClientGuiProperties.collect(handlerType(handler), properties.size(), id -> properties.get(id).get());
        publicProperties.entrySet().forEach(entry -> value.add(entry.getKey(), entry.getValue()));
        value.add("focused_slot_id", JsonNull.INSTANCE);
        return value;
    }

    private static String handlerType(ScreenHandler handler) {
        if (handler instanceof PlayerScreenHandler) return "minecraft:player";
        return Registries.SCREEN_HANDLER.getId(handler.getType()).toString();
    }

    private static String screenKind(ScreenHandler handler) {
        if (handler instanceof PlayerScreenHandler) return "player_inventory";
        if (handler instanceof GenericContainerScreenHandler) return "generic_container";
        if (handler instanceof CraftingScreenHandler) return "crafting";
        if (handler instanceof AbstractFurnaceScreenHandler) return "furnace";
        if (handler instanceof MerchantScreenHandler) return "merchant";
        if (handler instanceof AnvilScreenHandler) return "anvil";
        if (handler instanceof EnchantmentScreenHandler) return "enchanting";
        if (handler instanceof BeaconScreenHandler) return "beacon";
        if (handler instanceof HopperScreenHandler) return "hopper";
        if (handler instanceof ShulkerBoxScreenHandler) return "shulker_box";
        if (handler instanceof HorseScreenHandler) return "horse";
        return "unknown";
    }

    private static String slotSourceKind(Slot slot, ClientPlayerEntity player) {
        if (slot.inventory == player.getInventory()) {
            int index = slot.getIndex();
            if (index >= 0 && index <= 8) return "player_hotbar";
            if (index >= 9 && index <= 35) return "player_main";
            if (index >= 36 && index <= 39) return "player_armor";
            if (index == PlayerInventory.OFF_HAND_SLOT) return "player_offhand";
            return "unknown";
        }
        if (slot.inventory instanceof CraftingResultInventory
                || slot instanceof CraftingResultSlot
                || slot instanceof CrafterOutputSlot
                || slot instanceof FurnaceOutputSlot
                || slot instanceof TradeOutputSlot) {
            return "output";
        }
        if (slot.inventory instanceof CraftingInventory) return "input";
        return "container";
    }

    static JsonObject perceptionMetadata() {
        JsonObject value = ClientObservationJson.object();
        value.addProperty("sensor_profile_revision", SENSOR_PROFILE_REVISION);
        value.addProperty("horizontal_fov_degrees", HORIZONTAL_FOV);
        value.addProperty("vertical_fov_degrees", VERTICAL_FOV);
        value.addProperty("ray_columns", RAY_COLUMNS);
        value.addProperty("ray_rows", RAY_ROWS);
        value.addProperty("max_block_distance", BLOCK_MAX_DISTANCE);
        value.addProperty("body_expansion_blocks", BODY_EXPANSION);
        value.addProperty("block_epsilon_blocks", 0.001);
        value.addProperty("entity_max_distance", ENTITY_MAX_DISTANCE);
        value.addProperty("entity_occlusion_epsilon_blocks", ENTITY_OCCLUSION_EPSILON);
        return value;
    }

    private static double rayYawOffset(int column) {
        return -HORIZONTAL_FOV / 2.0 + column * (HORIZONTAL_FOV / (RAY_COLUMNS - 1));
    }

    private static double rayPitchOffset(int row) {
        return -VERTICAL_FOV / 2.0 + row * (VERTICAL_FOV / (RAY_ROWS - 1));
    }

    static Vec3d rayDirection(float yaw, float pitch, int row, int column) {
        return Vec3d.fromPolar(pitch + (float) rayPitchOffset(row), yaw + (float) rayYawOffset(column));
    }

    private static int rayId(int row, int column) {
        return row * RAY_COLUMNS + column;
    }

    private static boolean withinEntityFov(double yawOffset, double pitchOffset) {
        return Math.abs(yawOffset) <= HORIZONTAL_FOV / 2.0 && Math.abs(pitchOffset) <= VERTICAL_FOV / 2.0;
    }

    private static JsonObject collectPerception(
            MinecraftClient client,
            ClientPlayerEntity player,
            long generationId) {
        JsonObject value = perceptionMetadata();
        Vec3d camera = player.getCameraPosVec(1.0f);
        JsonArray rays = ClientObservationJson.array();
        for (int row = 0; row < RAY_ROWS; row++) {
            for (int column = 0; column < RAY_COLUMNS; column++) {
                rays.add(collectBlockRay(client, player, camera, row, column));
            }
        }
        value.add("block_rays", rays);
        value.add("body_contacts", collectBodyContacts(client, player));
        VisibleEntityResult entities = collectVisibleEntities(client, player, camera, generationId);
        value.add("visible_entities", entities.values());
        value.addProperty("entities_truncated", entities.truncatedCount() > 0);
        value.addProperty("truncated_entity_count", entities.truncatedCount());
        return value;
    }

    private static JsonObject collectBlockRay(
            MinecraftClient client,
            ClientPlayerEntity player,
            Vec3d camera,
            int row,
            int column) {
        double yawOffset = rayYawOffset(column);
        double pitchOffset = rayPitchOffset(row);
        BlockHitResult hit = legacyBlockHit(client.world, player, camera,
                player.getYaw(), player.getPitch(), row, column);
        JsonObject ray = ClientObservationJson.object();
        ray.addProperty("ray_id", rayId(row, column));
        ray.addProperty("row", row);
        ray.addProperty("column", column);
        ray.addProperty("yaw_offset_degrees", yawOffset);
        ray.addProperty("pitch_offset_degrees", pitchOffset);
        if (hit.getType() != HitResult.Type.BLOCK) {
            addMissRayFields(ray);
            return ray;
        }
        BlockPos position = hit.getBlockPos();
        BlockState state = client.world.getBlockState(position);
        ray.addProperty("hit_kind", "block");
        ray.addProperty("distance_blocks", Math.min(BLOCK_MAX_DISTANCE, camera.distanceTo(hit.getPos())));
        ray.add("relative_block_position", relativeBlockPosition(position, player.getPos()));
        ray.add("relative_hit_position", vector(hit.getPos().subtract(player.getPos())));
        ray.addProperty("face", hit.getSide().asString());
        ray.addProperty("block_id", Registries.BLOCK.getId(state.getBlock()).toString());
        if (state.getFluidState().isEmpty()) {
            ray.add("fluid_id", JsonNull.INSTANCE);
        } else {
            ray.addProperty(
                    "fluid_id",
                    Registries.FLUID.getId(state.getFluidState().getFluid()).toString());
        }
        ray.add("state_properties", stateProperties(state));
        ray.addProperty("collision_shape", collisionSummary(state.getCollisionShape(client.world, position)));
        ray.addProperty("block_light", client.world.getLightLevel(LightType.BLOCK, position));
        ray.addProperty("sky_light", client.world.getLightLevel(LightType.SKY, position));
        return ray;
    }

    /** Exact V2 first-hit calculation shared with the opt-in set-only parity scan. */
    static BlockHitResult legacyBlockHit(BlockView world, Entity observer, Vec3d camera,
            float yaw, float pitch, int row, int column) {
        Vec3d direction = rayDirection(yaw, pitch, row, column);
        Vec3d end = camera.add(direction.multiply(BLOCK_MAX_DISTANCE));
        return world.raycast(new RaycastContext(camera, end,
            RaycastContext.ShapeType.OUTLINE, RaycastContext.FluidHandling.ANY, observer));
    }

    /** Test-only second scan: positions only, never the historical 1431-ray JSON shell. */
    static Set<BlockPos> legacyFirstHitPositions(BlockView world, Entity observer, Vec3d camera,
            float yaw, float pitch) {
        var result = new HashSet<BlockPos>();
        for (int row=0; row<RAY_ROWS; row++) {
            for (int column=0; column<RAY_COLUMNS; column++) {
                BlockHitResult hit = legacyBlockHit(world, observer, camera, yaw, pitch, row, column);
                if (hit.getType()==HitResult.Type.BLOCK) result.add(hit.getBlockPos().toImmutable());
            }
        }
        if (result.size()>RAY_COLUMNS*RAY_ROWS) throw new IllegalStateException("legacy first-hit set exceeds ray budget");
        return result;
    }

    private static void addMissRayFields(JsonObject ray) {
        ray.addProperty("hit_kind", "miss");
        ray.addProperty("distance_blocks", BLOCK_MAX_DISTANCE);
        ray.add("relative_block_position", JsonNull.INSTANCE);
        ray.add("relative_hit_position", JsonNull.INSTANCE);
        ray.add("face", JsonNull.INSTANCE);
        ray.add("block_id", JsonNull.INSTANCE);
        ray.add("fluid_id", JsonNull.INSTANCE);
        ray.add("state_properties", ClientObservationJson.array());
        ray.add("collision_shape", JsonNull.INSTANCE);
        ray.add("block_light", JsonNull.INSTANCE);
        ray.add("sky_light", JsonNull.INSTANCE);
    }

    private static JsonArray stateProperties(BlockState state) {
        List<Property<?>> properties = new ArrayList<>(state.getProperties());
        properties.sort(Comparator.comparing(Property::getName));
        JsonArray values = ClientObservationJson.array();
        for (Property<?> property : properties) {
            JsonObject value = ClientObservationJson.object();
            value.addProperty("name", property.getName());
            value.addProperty("value", propertyValue(state, property));
            values.add(value);
        }
        return values;
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private static String propertyValue(BlockState state, Property property) {
        return property.name(state.get(property));
    }

    private static String collisionSummary(VoxelShape shape) {
        if (shape.isEmpty()) return "empty";
        return Block.isShapeFullCube(shape) ? "solid" : "partial";
    }

    static boolean intersectsCollisionShape(
            Box body,
            BlockPos position,
            VoxelShape collision) {
        if (collision.isEmpty()) return false;
        for (Box part : collision.getBoundingBoxes()) {
            if (part.offset(position).intersects(body)) return true;
        }
        return false;
    }

    private static JsonArray collectBodyContacts(
            MinecraftClient client,
            ClientPlayerEntity player) {
        JsonArray result = ClientObservationJson.array();
        Box body = player.getBoundingBox().expand(BODY_EXPANSION);
        BlockPos minimum = BlockPos.ofFloored(body.minX, body.minY, body.minZ);
        BlockPos maximum = BlockPos.ofFloored(body.maxX, body.maxY, body.maxZ);
        for (BlockPos mutable : BlockPos.iterate(minimum, maximum)) {
            BlockPos position = mutable.toImmutable();
            BlockState state = client.world.getBlockState(position);
            VoxelShape collision = state.getCollisionShape(client.world, position);
            boolean collisionContact = intersectsCollisionShape(body, position, collision);
            boolean fluidContact = false;
            if (!state.getFluidState().isEmpty()) {
                double height = state.getFluidState().getHeight(client.world, position);
                fluidContact = new Box(
                                position.getX(),
                                position.getY(),
                                position.getZ(),
                                position.getX() + 1.0,
                                position.getY() + height,
                                position.getZ() + 1.0)
                        .intersects(body);
            }
            if (!collisionContact && !fluidContact) continue;
            JsonObject contact = ClientObservationJson.object();
            contact.add("relative_block_position", relativeBlockPosition(position, player.getPos()));
            contact.addProperty("block_id", Registries.BLOCK.getId(state.getBlock()).toString());
            if (state.getFluidState().isEmpty()) {
                contact.add("fluid_id", JsonNull.INSTANCE);
            } else {
                contact.addProperty(
                        "fluid_id",
                        Registries.FLUID.getId(state.getFluidState().getFluid()).toString());
            }
            contact.addProperty("collision_shape", collisionSummary(collision));
            result.add(contact);
        }
        return result;
    }

    private static VisibleEntityResult collectVisibleEntities(
            MinecraftClient client,
            ClientPlayerEntity player,
            Vec3d camera,
            long generationId) {
        ENTITY_INDEX.beginFrame(client.world, generationId, client.world.getEntities());
        return visibleEntitiesInFrame(client,player,camera,ENTITY_INDEX,currentTarget(client,player));
    }

    static HitResult currentTarget(MinecraftClient client, ClientPlayerEntity player) {
        Entity cameraEntity = client.getCameraEntity();
        if (!(client.gameRenderer instanceof ClientCrosshairAccess crosshairAccess)) {
            throw new IllegalStateException("read-only native crosshair query unavailable");
        }
        return cameraEntity == null ? null : crosshairAccess.mc2p$findCrosshairTarget(
                cameraEntity, player.getBlockInteractionRange(), player.getEntityInteractionRange(), 1.0f);
    }

    static VisibleEntityResult collectVisibleEntities(MinecraftClient client, ClientPlayerEntity player,
            Vec3d camera, long generationId, ClientEntityIndex index, HitResult crosshair) {
        index.beginFrame(client.world,generationId,client.world.getEntities());
        return visibleEntitiesInFrame(client,player,camera,index,crosshair);
    }

    private static VisibleEntityResult visibleEntitiesInFrame(MinecraftClient client, ClientPlayerEntity player,
            Vec3d camera, ClientEntityIndex index, HitResult crosshair) {
        Entity cameraEntity = client.getCameraEntity();
        Entity targetedEntity = crosshair instanceof EntityHitResult hit ? hit.getEntity() : null;
        for (Entity entity : client.world.getEntities()) {
            if (entity == player || entity.isRemoved() || entity.isInvisibleTo(player)) continue;
            Vec3d relative = entity.getPos().subtract(player.getPos());
            double distanceSquared = relative.lengthSquared();
            if (distanceSquared > ENTITY_MAX_DISTANCE * ENTITY_MAX_DISTANCE) continue;
            Vec3d aim = entity.getBoundingBox().getCenter().subtract(camera);
            double horizontal = Math.sqrt(aim.x * aim.x + aim.z * aim.z);
            double bearingYaw = Math.toDegrees(Math.atan2(-aim.x, aim.z));
            double bearingPitch = -Math.toDegrees(Math.atan2(aim.y, horizontal));
            double yawOffset = MathHelper.wrapDegrees(bearingYaw - player.getYaw());
            double pitchOffset = bearingPitch - player.getPitch();
            if (!withinEntityFov(yawOffset, pitchOffset)) continue;
            if (!isEntityVisible(client, player, camera, entity.getBoundingBox())) continue;
            String type = Registries.ENTITY_TYPE.getId(entity.getType()).toString();
            index.offer(entity, relative, type);
        }
        JsonArray values = ClientObservationJson.array();
        for (ClientEntityIndex.Candidate candidate : index.selected()) {
            String name = ClientEntityName.visibleName(candidate.entity(), camera, cameraEntity,
                    targetedEntity, player.getScoreboardTeam(), MinecraftClient.isHudEnabled(), true);
            values.add(visibleEntity(player, candidate, candidate.trackId(), name));
        }
        return new VisibleEntityResult(values, index.truncatedCount());
    }

    private static boolean isEntityVisible(
            MinecraftClient client,
            ClientPlayerEntity player,
            Vec3d camera,
            Box box) {
        double midY = (box.minY + box.maxY) * 0.5;
        double midX = (box.minX + box.maxX) * 0.5;
        double midZ = (box.minZ + box.maxZ) * 0.5;
        Vec3d[] samples = new Vec3d[] {
            box.getCenter(),
            new Vec3d(midX, box.minY + box.getLengthY() * 0.85, midZ),
            box.getBottomCenter(),
            new Vec3d(box.minX + 0.001, midY, midZ),
            new Vec3d(box.maxX - 0.001, midY, midZ),
        };
        if (samples.length != ENTITY_VISIBILITY_SAMPLE_COUNT) {
            throw new IllegalStateException("entity visibility sample count changed");
        }
        for (Vec3d sample : samples) {
            BlockHitResult hit = client.world.raycast(new RaycastContext(
                    camera,
                    sample,
                    RaycastContext.ShapeType.OUTLINE,
                    RaycastContext.FluidHandling.NONE,
                    player));
            if (hit.getType() == HitResult.Type.MISS
                    || camera.distanceTo(hit.getPos()) + ENTITY_OCCLUSION_EPSILON
                            >= camera.distanceTo(sample)) {
                return true;
            }
        }
        return false;
    }

    private static JsonObject visibleEntity(
            ClientPlayerEntity player,
            ClientEntityIndex.Candidate candidate,
            String trackId,
            String displayName) {
        Entity entity = candidate.entity();
        Box box = entity.getBoundingBox();
        JsonObject value = ClientObservationJson.object();
        value.addProperty("track_id", trackId);
        value.addProperty("entity_type", candidate.entityType());
        if (displayName == null) value.add("display_name", JsonNull.INSTANCE);
        else value.addProperty("display_name", displayName);
        value.add("relative_position", vector(candidate.relativePosition()));
        value.add("relative_velocity", vector(entity.getVelocity().subtract(player.getVelocity())));
        value.addProperty(
                "relative_yaw_degrees",
                MathHelper.wrapDegrees(entity.getYaw() - player.getYaw()));
        value.addProperty("pitch_degrees", entity.getPitch());
        value.add("bounding_box_size", vector(new Vec3d(
                box.getLengthX(), box.getLengthY(), box.getLengthZ())));
        value.addProperty("pose", entity.getPose().name().toLowerCase(Locale.ROOT));
        value.addProperty("is_on_ground", entity.isOnGround());
        value.add("equipment", visibleEquipment(entity));
        value.addProperty("hurt_animation_ticks",
                entity instanceof LivingEntity living ? living.hurtTime : 0);
        return value;
    }

    private static JsonArray visibleEquipment(Entity entity) {
        JsonArray values = ClientObservationJson.array();
        if (!(entity instanceof LivingEntity living)) return values;
        // Pinned vanilla CowEntityRenderer has no held-item, armor, or head-item feature.
        // The server may synchronize those slots anyway; even their emptiness is not a visible slot.
        // This exact-type counterexample fix is NOT a general renderer/partial-slot visibility gate.
        if (entity.getType() == EntityType.COW) return values;
        EquipmentSlot[] slots = new EquipmentSlot[] {
            EquipmentSlot.MAINHAND,
            EquipmentSlot.OFFHAND,
            EquipmentSlot.HEAD,
            EquipmentSlot.CHEST,
            EquipmentSlot.LEGS,
            EquipmentSlot.FEET,
        };
        for (EquipmentSlot slot : slots) {
            JsonObject value = ClientObservationJson.object();
            value.addProperty("slot", equipmentSlotName(slot));
            value.add("item", visibleEquipmentItem(living.getEquippedStack(slot)));
            values.add(value);
        }
        return values;
    }

    private static JsonObject visibleEquipmentItem(ItemStack stack) {
        JsonObject value = ClientObservationJson.object();
        boolean empty = stack.isEmpty();
        value.addProperty("empty", empty);
        if (empty) value.add("item_id", JsonNull.INSTANCE);
        else value.addProperty("item_id", Registries.ITEM.getId(stack.getItem()).toString());
        return value;
    }

    private static String equipmentSlotName(EquipmentSlot slot) {
        return switch (slot) {
            case MAINHAND -> "main_hand";
            case OFFHAND -> "off_hand";
            case HEAD -> "head";
            case CHEST -> "chest";
            case LEGS -> "legs";
            case FEET -> "feet";
            default -> throw new IllegalArgumentException("unsupported public equipment slot");
        };
    }

    private static JsonObject relativeBlockPosition(BlockPos block, Vec3d origin) {
        return vector(new Vec3d(
                block.getX() - origin.x,
                block.getY() - origin.y,
                block.getZ() - origin.z));
    }

    record VisibleEntityResult(JsonArray values, int truncatedCount) {}

    static JsonObject vector(Vec3d vector) {
        JsonObject value = ClientObservationJson.object();
        value.addProperty("x", vector.x);
        value.addProperty("y", vector.y);
        value.addProperty("z", vector.z);
        return value;
    }
}
