package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.LockstepClientSimulationGate;
import net.minecraft.client.network.ClientPlayerEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Installed only in isolated lockstep sandboxes, never in deployment/reference clients. */
@Mixin(ClientPlayerEntity.class)
public abstract class LockstepPlayerTickMixin {
    @Inject(method = "tick", at = @At("HEAD"), cancellable = true)
    private void skipSettleSimulation(CallbackInfo ci) {
        if (!LockstepClientSimulationGate.allowPlayerTick()) ci.cancel();
    }
}
