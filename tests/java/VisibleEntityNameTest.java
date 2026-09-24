import com.mc2p.observation.ClientEntityName;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.component.DataComponentTypes;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.LightningEntity;
import net.minecraft.entity.ItemEntity;
import net.minecraft.entity.OminousItemSpawnerEntity;
import net.minecraft.entity.decoration.DisplayEntity;
import net.minecraft.entity.FallingBlockEntity;
import net.minecraft.entity.mob.EvokerFangsEntity;
import net.minecraft.entity.projectile.FishingBobberEntity;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.entity.decoration.ItemFrameEntity;
import net.minecraft.entity.passive.CowEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.item.Items;
import net.minecraft.scoreboard.AbstractTeam;
import net.minecraft.scoreboard.Scoreboard;
import net.minecraft.scoreboard.Team;
import net.minecraft.text.Text;
import net.minecraft.util.Arm;
import net.minecraft.util.math.Vec3d;

/** Detached vanilla state; only world-backed scoreboard/item access is supplied locally. */
public class VisibleEntityNameTest {
    private static final Vec3d CAMERA = new Vec3d(0, 0, 4);
    private static DetachedTestWorld world;
    private static final java.util.List<String> errors = new java.util.ArrayList<>();
    private static final class Cow extends CowEntity {
        Team team;
        Cow() { super(EntityType.COW, world); }
        @Override public Team getScoreboardTeam() { return team; }
    }
    private static final class Stand extends ArmorStandEntity {
        Stand() { super(EntityType.ARMOR_STAND, null); }
        @Override public Team getScoreboardTeam() { return null; }
    }
    private static final class Frame extends ItemFrameEntity {
        ItemStack stack = ItemStack.EMPTY;
        Frame() { super(EntityType.ITEM_FRAME, null); }
        @Override public Team getScoreboardTeam() { return null; }
        @Override public ItemStack getHeldItemStack() { return stack; }
    }
    private static final class Living extends LivingEntity {
        Team team;
        boolean passengers;
        Living() { super(EntityType.PLAYER, null); }
        @Override public Team getScoreboardTeam() { return team; }
        @Override public Iterable<ItemStack> getArmorItems() { return java.util.List.of(); }
        @Override public ItemStack getEquippedStack(EquipmentSlot slot) { return ItemStack.EMPTY; }
        @Override public void equipStack(EquipmentSlot slot, ItemStack item) {}
        @Override public Arm getMainArm() { return Arm.RIGHT; }
        @Override public boolean hasPassengers() { return passengers; }
    }

    public static void main(String[] args) throws Exception {
        SharedConstants.createGameVersion();
        Bootstrap.initialize();
        world = new DetachedTestWorld();
        Cow cow = new Cow();
        cow.setCustomName(Text.literal("secret cow"));
        expect(null, cow, CAMERA, null, null, null, true, true, "untargeted custom mob");
        expect("secret cow", cow, CAMERA, null, cow, null, true, true, "targeted custom mob");
        cow.setCustomNameVisible(true);
        expect("secret cow", cow, CAMERA, null, null, null, true, true, "always visible mob name");
        expect(null, cow, CAMERA, null, cow, null, false, true, "HUD disabled for unteamed mob");
        expect(null, cow, CAMERA, null, cow, null, true, false, "invisible to observer");
        cow.setSneaking(true);
        expect("secret cow", cow, new Vec3d(0, 0, 31.999), null, cow, null, true, true, "sneak near");
        expect(null, cow, new Vec3d(0, 0, 32), null, cow, null, true, true, "sneak boundary");
        cow.setSneaking(false);
        expect(null, cow, new Vec3d(0, 0, 64), null, cow, null, true, true, "normal boundary");

        Scoreboard scoreboard = new Scoreboard();
        Team red = scoreboard.addTeam("red"), blue = scoreboard.addTeam("blue");
        cow.team = red;
        red.setNameTagVisibilityRule(AbstractTeam.VisibilityRule.NEVER);
        expect(null, cow, CAMERA, null, cow, red, true, true, "team never");
        red.setNameTagVisibilityRule(AbstractTeam.VisibilityRule.HIDE_FOR_OTHER_TEAMS);
        expect("secret cow", cow, CAMERA, null, cow, red, true, true, "same team");
        expect(null, cow, CAMERA, null, cow, blue, true, true, "other team hidden");
        expect("secret cow", cow, CAMERA, null, cow, null, true, true, "unteamed observer");
        red.setNameTagVisibilityRule(AbstractTeam.VisibilityRule.HIDE_FOR_OWN_TEAM);
        expect(null, cow, CAMERA, null, cow, red, true, true, "own team hidden");
        expect("secret cow", cow, CAMERA, null, cow, blue, true, true, "other team allowed");
        red.setNameTagVisibilityRule(AbstractTeam.VisibilityRule.ALWAYS);
        expect("secret cow", cow, CAMERA, null, cow, red, false, true, "native team branch before HUD");
        cow.setCustomNameVisible(false);
        expect(null, cow, CAMERA, null, null, red, true, true, "team does not bypass mob target rule");

        Living living = new Living();
        living.setCustomName(Text.literal("public player"));
        expect("public player", living, CAMERA, null, null, null, true, true, "living does not need target");
        expect(null, living, CAMERA, living, null, null, true, true, "camera entity");
        living.passengers = true;
        expect(null, living, CAMERA, null, null, null, true, true, "unteamed passenger carrier");

        Stand stand = new Stand();
        stand.setCustomName(Text.literal("stand label"));
        expect(null, stand, CAMERA, null, stand, null, true, true, "stand target not sufficient");
        stand.setCustomNameVisible(true);
        expect("stand label", stand, CAMERA, null, null, null, false, true, "stand visible flag independent of HUD");

        Frame frame = new Frame();
        frame.setCustomName(Text.literal("hidden frame name"));
        frame.stack = new ItemStack(Items.DIAMOND_SWORD);
        expect(null, frame, CAMERA, null, frame, null, true, true, "frame stack has no custom label");
        frame.stack.set(DataComponentTypes.CUSTOM_NAME, Text.literal("displayed item name"));
        expect("displayed item name", frame, CAMERA, null, frame, null, true, true, "frame uses item label, not entity name");
        expect(null, frame, CAMERA, null, null, null, true, true, "frame untargeted");
        expect(null, frame, CAMERA, null, frame, null, false, true, "frame HUD off");

        ItemEntity item = new ItemEntity(EntityType.ITEM, world);
        item.setCustomName(Text.literal("public item label"));
        expect(null, item, CAMERA, null, null, null, true, true, "base untargeted label");
        expect("public item label", item, CAMERA, null, item, null, true, true, "base targeted label");
        item.setCustomNameVisible(true);
        expect("public item label", item, new Vec3d(0, 0, 64), null, null, null, true, true, "base inclusive 64 range");
        expect(null, item, new Vec3d(0, 0, 64.001), null, null, null, true, true, "base outside range");
        item.setCustomName(Text.literal(""));
        expect(null, item, CAMERA, null, item, null, true, true, "empty label normalized");
        LightningEntity lightning = new LightningEntity(EntityType.LIGHTNING_BOLT, world);
        lightning.setCustomName(Text.literal("never rendered lightning name"));
        lightning.setCustomNameVisible(true);
        expect(null, lightning, CAMERA, null, lightning, null, true, true, "lightning renderer has no label path");
        OminousItemSpawnerEntity spawner = new OminousItemSpawnerEntity(EntityType.OMINOUS_ITEM_SPAWNER, world);
        spawner.setCustomName(Text.literal("never rendered spawner name"));
        spawner.setCustomNameVisible(true);
        expect(null, spawner, CAMERA, null, spawner, null, true, true, "ominous spawner renderer has no label path");
        for (DisplayEntity display : java.util.List.of(
                new DisplayEntity.ItemDisplayEntity(EntityType.ITEM_DISPLAY, world),
                new DisplayEntity.BlockDisplayEntity(EntityType.BLOCK_DISPLAY, world),
                new DisplayEntity.TextDisplayEntity(EntityType.TEXT_DISPLAY, world))) {
            display.setCustomName(Text.literal("uninitialized display name"));
            display.setCustomNameVisible(true);
            expect(null, display, CAMERA, null, display, null, true, true, "display without client presentation state");
            // A newly constructed detached entity has received no tracked-data update. Mark that
            // fixture state, then let the actual client-tick implementation create its own data.
            setField(DisplayEntity.class, display, "renderingDataSet", true);
            display.tick();
            if (display.getRenderState() == null) throw new AssertionError("display fixture did not initialize");
            expect("uninitialized display name", display, CAMERA, null, display, null, true, true,
                    "initialized display still labels empty presentation content");
            setField(display.getClass(), display, "data", null);
            expect(null, display, CAMERA, null, display, null, true, true,
                    "display with render state but absent subtype data");
        }
        FallingBlockEntity falling = new FallingBlockEntity(EntityType.FALLING_BLOCK, world);
        falling.setCustomName(Text.literal("falling label"));
        falling.setCustomNameVisible(true);
        expect("falling label", falling, CAMERA, null, falling, null, true, true, "visible falling model");
        world.blocks.put(falling.getBlockPos(), falling.getBlockState());
        expect(null, falling, CAMERA, null, falling, null, true, true, "falling state matches world block");
        world.blocks.clear();
        setField(FallingBlockEntity.class, falling, "block", net.minecraft.block.Blocks.AIR.getDefaultState());
        expect(null, falling, CAMERA, null, falling, null, true, true, "non-model falling block");
        EvokerFangsEntity fangs = new EvokerFangsEntity(EntityType.EVOKER_FANGS, world);
        fangs.setCustomName(Text.literal("fangs label"));
        fangs.setCustomNameVisible(true);
        expect(null, fangs, CAMERA, null, fangs, null, true, true, "fangs animation not playing");
        setField(EvokerFangsEntity.class, fangs, "playingAnimation", true);
        expect("fangs label", fangs, CAMERA, null, fangs, null, true, true, "fangs animation playing");
        FishingBobberEntity bobber = new FishingBobberEntity(EntityType.FISHING_BOBBER, world);
        bobber.setCustomName(Text.literal("bobber label"));
        bobber.setCustomNameVisible(true);
        expect(null, bobber, CAMERA, null, bobber, null, true, true, "bobber owner absent");
        PlayerEntity owner = new PlayerEntity(world, net.minecraft.util.math.BlockPos.ORIGIN, 0,
                new com.mojang.authlib.GameProfile(new java.util.UUID(0, 1), "PublicPlayer")) {
            @Override public boolean isSpectator() { return false; }
            @Override public boolean isCreative() { return false; }
        };
        bobber.setOwner(owner);
        expect("bobber label", bobber, CAMERA, owner, bobber, null, true, true, "bobber owner present");
        expect("PublicPlayer", owner, CAMERA, null, null, null, true, true, "ordinary player name without targeting");
        for (EntityType<?> type : java.util.List.of(EntityType.EGG, EntityType.ENDER_PEARL,
                EntityType.EXPERIENCE_BOTTLE, EntityType.EYE_OF_ENDER, EntityType.FIREBALL,
                EntityType.POTION, EntityType.SMALL_FIREBALL, EntityType.SNOWBALL,
                EntityType.WIND_CHARGE, EntityType.BREEZE_WIND_CHARGE)) {
            Entity projectile = type.create(world);
            projectile.setCustomName(Text.literal("projectile label"));
            projectile.setCustomNameVisible(true);
            owner.setPos(3.499, 0, 0);
            expect(null, projectile, CAMERA, owner, projectile, null, true, true, "new close projectile: " + type);
            owner.setPos(3.5, 0, 0);
            expect("projectile label", projectile, CAMERA, owner, projectile, null, true, true, "new projectile distance boundary: " + type);
            owner.setPos(0, 0, 0);
            projectile.age = 2;
            expect("projectile label", projectile, CAMERA, owner, projectile, null, true, true, "projectile age boundary: " + type);
        }
        Entity dragonFireball = EntityType.DRAGON_FIREBALL.create(world);
        dragonFireball.setCustomName(Text.literal("dragon fireball label"));
        dragonFireball.setCustomNameVisible(true);
        expect("dragon fireball label", dragonFireball, CAMERA, owner, dragonFireball, null, true, true,
                "dedicated dragon-fireball renderer does not use flying-item age rule");
        if (!errors.isEmpty()) throw new AssertionError(String.join("; ", errors));
        System.out.println("VISIBLE_ENTITY_NAME_OK");
    }

    private static void setField(Class<?> type, Object instance, String name, Object value) throws Exception {
        var field = type.getDeclaredField(name);
        field.setAccessible(true);
        field.set(instance, value);
    }

    private static void expect(String wanted, Entity target, Vec3d camera, Entity cameraEntity,
            Entity targeted, AbstractTeam observerTeam, boolean hud, boolean visible, String reason) {
        String actual = ClientEntityName.visibleName(target, camera, cameraEntity, targeted,
                observerTeam, hud, visible);
        if (!java.util.Objects.equals(wanted, actual))
            errors.add(reason + ": expected " + wanted + ", got " + actual);
    }
}
