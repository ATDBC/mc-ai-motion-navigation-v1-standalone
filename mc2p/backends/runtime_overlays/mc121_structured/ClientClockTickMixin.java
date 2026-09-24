package com.kyhsgeekcode.minecraftenv.mixin;

import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(MinecraftClient.class)
public abstract class ClientClockTickMixin {
    @Inject(method = "tick", at = @At("HEAD"))
    private void mc2p$countClientTick(CallbackInfo ci) { ClientTimeDiagnostics.clientTick(); }
}
