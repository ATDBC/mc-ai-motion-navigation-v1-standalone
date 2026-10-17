package com.mc2p.observation;

import net.minecraft.entity.Entity;
import net.minecraft.util.hit.HitResult;

/** Read-only vanilla query. Does not update crosshairTarget, targetedEntity or renderer caches. */
public interface ClientCrosshairAccess {
    HitResult mc2p$findCrosshairTarget(Entity cameraEntity, double blockRange, double entityRange,
            float tickDelta);
}
