package com.mc2p.deployment.mixin;

import com.mc2p.surface.DirtyTracker;
import net.minecraft.block.BlockState;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.chunk.WorldChunk;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/** Invalidates only cached surface tiles touched by a client-world block change. */
@Mixin(WorldChunk.class)
public abstract class SurfaceChunkStateMixin {
    @Inject(method="setBlockState", at=@At("RETURN"))
    private void mc2pSurfaceChanged(BlockPos position, BlockState state, boolean moved,
            CallbackInfoReturnable<BlockState> result) {
        if (result.getReturnValue() != null) {
            WorldChunk chunk = (WorldChunk)(Object)this;
            DirtyTracker.block(chunk.getWorld(), position.getX(), position.getY(), position.getZ());
        }
    }
}
