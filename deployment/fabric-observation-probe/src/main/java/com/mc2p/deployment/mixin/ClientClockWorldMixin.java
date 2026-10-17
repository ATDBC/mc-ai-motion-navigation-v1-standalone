package com.mc2p.deployment.mixin;

import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.world.ClientWorld;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ClientWorld.class)
public abstract class ClientClockWorldMixin {
    @Unique private long mc2p$beforeTickTime;
    @Inject(method = "tickTime", at = @At("HEAD"))
    private void mc2p$beforeTickTime(CallbackInfo ci) {
        if (ClientTimeDiagnostics.onClientThread()) mc2p$beforeTickTime = ((ClientWorld) (Object) this).getTime();
    }
    @Inject(method = "tickTime", at = @At("TAIL"))
    private void mc2p$afterTickTime(CallbackInfo ci) {
        ClientTimeDiagnostics.worldTick((ClientWorld) (Object) this, mc2p$beforeTickTime);
    }
}
