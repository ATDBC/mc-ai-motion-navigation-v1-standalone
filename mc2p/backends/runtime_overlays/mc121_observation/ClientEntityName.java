package com.mc2p.observation;

import java.util.Set;
import net.minecraft.block.BlockRenderType;
import net.minecraft.component.DataComponentTypes;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityAttachmentType;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.FallingBlockEntity;
import net.minecraft.entity.LightningEntity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.OminousItemSpawnerEntity;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.entity.decoration.DisplayEntity;
import net.minecraft.entity.decoration.ItemFrameEntity;
import net.minecraft.entity.mob.EvokerFangsEntity;
import net.minecraft.entity.mob.MobEntity;
import net.minecraft.entity.projectile.FishingBobberEntity;
import net.minecraft.scoreboard.AbstractTeam;
import net.minecraft.text.Text;
import net.minecraft.util.math.Vec3d;

/** Read-only MC 1.21 name-label eligibility; no renderer instance or cached render context. */
public final class ClientEntityName {
    // Exact vanilla 1.21 renderer registrations, not the broader FlyingItemEntity interface.
    private static final Set<EntityType<?>> EARLY_HIDDEN_PROJECTILES = Set.of(
            EntityType.EGG, EntityType.ENDER_PEARL, EntityType.EXPERIENCE_BOTTLE,
            EntityType.EYE_OF_ENDER, EntityType.FIREBALL, EntityType.POTION,
            EntityType.SMALL_FIREBALL, EntityType.SNOWBALL,
            EntityType.WIND_CHARGE, EntityType.BREEZE_WIND_CHARGE);

    private ClientEntityName() {}

    /** Called only after the project's entity range, FOV and occlusion filter. */
    public static String visibleName(Entity entity, Vec3d camera, Entity cameraEntity,
            Entity targetedEntity, AbstractTeam observerTeam, boolean hudEnabled,
            boolean visibleToObserver) {
        if (!visibleToObserver || entity.isRemoved()
                || !outerLabelPathAvailable(entity, cameraEntity)) return null;
        double distanceSquared = camera.squaredDistanceTo(entity.getPos());
        if (distanceSquared > 4096.0 || entity.getAttachments().getPointNullable(
                EntityAttachmentType.NAME_TAG, 0, entity.getYaw()) == null) return null;

        Text text;
        if (entity instanceof ItemFrameEntity frame) {
            // Native ItemFrameEntityRenderer displays the held item's custom label, not the entity name.
            var stack = frame.getHeldItemStack();
            if (!hudEnabled || targetedEntity != entity || stack.isEmpty()
                    || !stack.contains(DataComponentTypes.CUSTOM_NAME)
                    || !insideLivingLabelRange(distanceSquared, entity.isSneaky())) return null;
            text = stack.getName();
        } else if (entity instanceof ArmorStandEntity stand) {
            // Armor stands override LivingEntityRenderer.hasLabel, including HUD/team behavior.
            if (!stand.isCustomNameVisible()
                    || !insideLivingLabelRange(distanceSquared, stand.isInSneakingPose())) return null;
            text = entity.getDisplayName();
        } else if (entity instanceof LivingEntity living) {
            if (!insideLivingLabelRange(distanceSquared, living.isSneaky())) return null;
            AbstractTeam team = living.getScoreboardTeam();
            if (team != null) {
                // In vanilla the team branch returns before the HUD/camera/passenger branch.
                boolean allowed = switch (team.getNameTagVisibilityRule()) {
                    case ALWAYS -> true;
                    case NEVER -> false;
                    case HIDE_FOR_OTHER_TEAMS -> observerTeam == null || team.isEqual(observerTeam);
                    case HIDE_FOR_OWN_TEAM -> observerTeam == null || !team.isEqual(observerTeam);
                };
                if (!allowed) return null;
            } else if (!hudEnabled || entity == cameraEntity || entity.hasPassengers()) return null;
            if (entity instanceof MobEntity && !baseLabelAllowed(entity, targetedEntity)) return null;
            text = entity.getDisplayName();
        } else {
            if (!baseLabelAllowed(entity, targetedEntity)) return null;
            text = entity.getDisplayName();
        }
        String value = text == null ? null : text.getString();
        return value == null || value.isEmpty() ? null : value;
    }

    private static boolean insideLivingLabelRange(double distanceSquared, boolean sneaky) {
        return distanceSquared < (sneaky ? 1024.0 : 4096.0);
    }

    private static boolean baseLabelAllowed(Entity entity, Entity targetedEntity) {
        return entity.shouldRenderName() || (entity.hasCustomName() && entity == targetedEntity);
    }

    private static boolean outerLabelPathAvailable(Entity entity, Entity cameraEntity) {
        // These renderers never call the base label path, irrespective of inherited name flags.
        if (entity instanceof LightningEntity || entity instanceof OminousItemSpawnerEntity) return false;
        if (entity instanceof DisplayEntity display) {
            // Entity client-tick presentation data, not frame/renderer caches. Empty content still labels.
            if (display.getRenderState() == null) return false;
            if (display instanceof DisplayEntity.BlockDisplayEntity block) return block.getData() != null;
            if (display instanceof DisplayEntity.ItemDisplayEntity item) return item.getData() != null;
            if (display instanceof DisplayEntity.TextDisplayEntity text) return text.getData() != null;
            return false;
        }
        if (entity instanceof FallingBlockEntity falling) {
            var state = falling.getBlockState();
            return state.getRenderType() == BlockRenderType.MODEL
                    && state != entity.getWorld().getBlockState(entity.getBlockPos());
        }
        if (entity instanceof FishingBobberEntity bobber) return bobber.getPlayerOwner() != null;
        if (entity instanceof EvokerFangsEntity fangs) return fangs.getAnimationProgress(1.0f) != 0.0f;
        if (EARLY_HIDDEN_PROJECTILES.contains(entity.getType()) && entity.age < 2) {
            // Native comparison uses the focused entity's feet position, not the camera eye vector.
            return cameraEntity != null && cameraEntity.squaredDistanceTo(entity) >= 12.25;
        }
        return true;
    }
}
