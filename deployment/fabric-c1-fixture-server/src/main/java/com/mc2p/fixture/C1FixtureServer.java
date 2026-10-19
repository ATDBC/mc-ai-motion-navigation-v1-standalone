package com.mc2p.fixture;

import com.mojang.brigadier.arguments.DoubleArgumentType;
import com.mojang.brigadier.arguments.LongArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.entity.event.v1.ServerLivingEntityEvents;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.mob.ZombieEntity;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

public final class C1FixtureServer implements ModInitializer {
    private static final Pattern SCENARIO = Pattern.compile("[a-z0-9-]{1,80}");
    private static final Pattern PHASE = Pattern.compile("[a-z0-9_]{1,40}");
    private static final String TAG_PREFIX = "mc2p-c1-fixture-";
    private static final Map<String, UUID> ACTIVE = new HashMap<>();
    private static final Map<String, Long> CANCELLED_DAMAGE = new HashMap<>();
    private static final Map<String, Long> REAL_DAMAGE = new HashMap<>();
    private static final Map<String, String> DAMAGE_MODES = new HashMap<>();

    @Override
    public void onInitialize() {
        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) -> {
            dispatcher.register(CommandManager.literal("mc2p_c1_spawn")
                .requires(source -> source.hasPermissionLevel(4))
                .then(CommandManager.argument("scenario", StringArgumentType.word())
                .then(CommandManager.argument("seed", LongArgumentType.longArg())
                .then(CommandManager.argument("x", DoubleArgumentType.doubleArg(-29_999_984, 29_999_984))
                .then(CommandManager.argument("y", DoubleArgumentType.doubleArg(-64, 320))
                .then(CommandManager.argument("z", DoubleArgumentType.doubleArg(-29_999_984, 29_999_984))
                .executes(context -> spawn(
                    context.getSource().getServer(), context.getSource().getWorld(),
                    StringArgumentType.getString(context, "scenario"),
                    LongArgumentType.getLong(context, "seed"),
                    DoubleArgumentType.getDouble(context, "x"),
                    DoubleArgumentType.getDouble(context, "y"),
                    DoubleArgumentType.getDouble(context, "z"), "isolated"))
                .then(CommandManager.argument("damage_mode", StringArgumentType.word())
                .executes(context -> spawn(
                    context.getSource().getServer(), context.getSource().getWorld(),
                    StringArgumentType.getString(context, "scenario"),
                    LongArgumentType.getLong(context, "seed"),
                    DoubleArgumentType.getDouble(context, "x"),
                    DoubleArgumentType.getDouble(context, "y"),
                    DoubleArgumentType.getDouble(context, "z"),
                    StringArgumentType.getString(context, "damage_mode"))))))))));
            dispatcher.register(CommandManager.literal("mc2p_c1_remove")
                .requires(source -> source.hasPermissionLevel(4))
                .then(CommandManager.argument("scenario", StringArgumentType.word())
                .executes(context -> remove(
                    context.getSource().getServer(),
                    StringArgumentType.getString(context, "scenario")))));
            dispatcher.register(CommandManager.literal("mc2p_c1_strike")
                .requires(source -> source.hasPermissionLevel(4))
                .then(CommandManager.argument("scenario", StringArgumentType.word())
                .then(CommandManager.argument("requested_phase", StringArgumentType.word())
                .executes(context -> strike(
                    context.getSource().getServer(),
                    StringArgumentType.getString(context, "scenario"),
                    StringArgumentType.getString(context, "requested_phase"))))));
        });
        ServerLivingEntityEvents.ALLOW_DAMAGE.register((entity, source, amount) -> {
            Entity attacker = source.getAttacker();
            if (!(entity instanceof ServerPlayerEntity)
                    || !(attacker instanceof ZombieEntity zombie)) {
                return true;
            }
            String scenario = scenarioFor(zombie);
            if (scenario == null || !ACTIVE.containsKey(scenario)) {
                return true;
            }
            String damageMode = DAMAGE_MODES.get(scenario);
            if ("real".equals(damageMode)) {
                long count = REAL_DAMAGE.merge(scenario, 1L, Long::sum);
                ServerWorld world = (ServerWorld) zombie.getWorld();
                append(zombie.getServer(), "{\"event\":\"damage_allowed\",\"scenario_id\":\""
                    + scenario + "\",\"server_tick\":" + world.getTime()
                    + ",\"amount\":" + amount + ",\"damage_event_count\":" + count + "}");
                return true;
            }
            long count = CANCELLED_DAMAGE.merge(scenario, 1L, Long::sum);
            append(zombie.getServer(), "{\"event\":\"damage_isolated\",\"scenario_id\":\""
                + scenario + "\",\"damage_isolation_count\":" + count + "}");
            return false;
        });
    }

    private static int spawn(MinecraftServer server, ServerWorld world, String scenario,
                             long seed, double x, double y, double z, String damageMode) {
        if (!SCENARIO.matcher(scenario).matches() || ACTIVE.containsKey(scenario)) {
            throw new IllegalArgumentException("invalid or duplicate C1 scenario id");
        }
        if (!("isolated".equals(damageMode) || "real".equals(damageMode))) {
            throw new IllegalArgumentException("invalid C1 damage mode");
        }
        ensureEvidenceWritable(server);
        ZombieEntity zombie = EntityType.ZOMBIE.create(world);
        if (zombie == null) throw new IllegalStateException("zombie creation failed");
        zombie.setBaby(false);
        zombie.setPersistent();
        zombie.refreshPositionAndAngles(x, y, z, 0.0F, 0.0F);
        zombie.addCommandTag(TAG_PREFIX + scenario);
        long spawnTick = world.getTime();
        zombie.getRandom().setSeed(seed);
        if (!world.spawnEntity(zombie)) throw new IllegalStateException("zombie spawn rejected");
        ACTIVE.put(scenario, zombie.getUuid());
        CANCELLED_DAMAGE.put(scenario, 0L);
        REAL_DAMAGE.put(scenario, 0L);
        DAMAGE_MODES.put(scenario, damageMode);
        try {
            append(server, "{\"event\":\"spawned\",\"scenario_id\":\"" + scenario
                + "\",\"entity_uuid\":\"" + zombie.getUuid() + "\",\"seed\":" + seed
                + ",\"spawn_tick\":" + spawnTick + ",\"seed_applied_tick\":" + spawnTick
                + ",\"seed_applied_before_first_ai_tick\":true,\"damage_mode\":\""
                + damageMode + "\",\"damage_isolation_count\":0,\"damage_event_count\":0}");
        } catch (RuntimeException error) {
            zombie.discard();
            ACTIVE.remove(scenario);
            CANCELLED_DAMAGE.remove(scenario);
            REAL_DAMAGE.remove(scenario);
            DAMAGE_MODES.remove(scenario);
            throw error;
        }
        return 1;
    }

    private static int remove(MinecraftServer server, String scenario) {
        UUID id = ACTIVE.remove(scenario);
        if (id == null) throw new IllegalArgumentException("unknown C1 scenario id");
        for (ServerWorld world : server.getWorlds()) {
            Entity entity = world.getEntity(id);
            if (entity != null) entity.discard();
        }
        long count = CANCELLED_DAMAGE.remove(scenario);
        long realCount = REAL_DAMAGE.remove(scenario);
        String damageMode = DAMAGE_MODES.remove(scenario);
        append(server, "{\"event\":\"removed\",\"scenario_id\":\"" + scenario
            + "\",\"damage_mode\":\"" + damageMode
            + "\",\"damage_isolation_count\":" + count
            + ",\"damage_event_count\":" + realCount + "}");
        return 1;
    }

    private static int strike(MinecraftServer server, String scenario, String requestedPhase) {
        if (!SCENARIO.matcher(scenario).matches() || !PHASE.matcher(requestedPhase).matches()) {
            throw new IllegalArgumentException("invalid controlled attack identity");
        }
        UUID id = ACTIVE.get(scenario);
        if (id == null || !"real".equals(DAMAGE_MODES.get(scenario))) {
            throw new IllegalArgumentException("controlled attack requires active real-damage fixture");
        }
        ZombieEntity zombie = null;
        for (ServerWorld world : server.getWorlds()) {
            Entity entity = world.getEntity(id);
            if (entity instanceof ZombieEntity found) {
                zombie = found;
                break;
            }
        }
        ServerPlayerEntity player = server.getPlayerManager().getPlayer("MC2PProbe");
        if (zombie == null || player == null || zombie.isRemoved() || !zombie.isAlive()) {
            throw new IllegalStateException("controlled attack participants unavailable");
        }
        zombie.setTarget(player);
        boolean success = zombie.tryAttack(player);
        ServerWorld world = (ServerWorld) zombie.getWorld();
        append(server, "{\"event\":\"controlled_attack\",\"scenario_id\":\""
            + scenario + "\",\"requested_phase\":\"" + requestedPhase
            + "\",\"server_tick\":" + world.getTime()
            + ",\"attacker_uuid\":\"" + zombie.getUuid()
            + "\",\"target_uuid\":\"" + player.getUuid()
            + "\",\"squared_distance\":" + zombie.squaredDistanceTo(player)
            + ",\"success\":" + success + "}");
        return success ? 1 : 0;
    }

    private static String scenarioFor(ZombieEntity zombie) {
        for (String tag : zombie.getCommandTags()) {
            if (tag.startsWith(TAG_PREFIX)) return tag.substring(TAG_PREFIX.length());
        }
        return null;
    }

    private static Path evidencePath(MinecraftServer server) {
        return server.getRunDirectory().resolve("c1-fixture-events.jsonl");
    }

    private static void ensureEvidenceWritable(MinecraftServer server) {
        Path path = evidencePath(server);
        try {
            Files.writeString(path, "", StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) {
            throw new IllegalStateException("C1 fixture evidence is not writable", error);
        }
    }

    private static void append(MinecraftServer server, String line) {
        try {
            Files.writeString(evidencePath(server), line + "\n", StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) {
            throw new IllegalStateException("C1 fixture evidence write failed", error);
        }
    }
}
