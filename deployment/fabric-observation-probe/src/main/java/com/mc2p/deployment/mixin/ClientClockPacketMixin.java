package com.mc2p.deployment.mixin;

import com.mc2p.diagnostics.ClientTimeDiagnostics;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayNetworkHandler;
import net.minecraft.network.packet.s2c.play.WorldTimeUpdateS2CPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ClientPlayNetworkHandler.class)
public abstract class ClientClockPacketMixin {
    @Unique private long mc2p$beforeTimeUpdate;
    @Inject(method = "onWorldTimeUpdate", at = @At("HEAD"))
    private void mc2p$beforeTimeUpdate(WorldTimeUpdateS2CPacket packet, CallbackInfo ci) {
        // The Netty call aborts in forceMainThread; capture only its actual main-thread application.
        if (ClientTimeDiagnostics.onClientThread()) mc2p$beforeTimeUpdate = MinecraftClient.getInstance().world.getTime();
    }
    @Inject(method = "onWorldTimeUpdate", at = @At("TAIL"))
    private void mc2p$afterTimeUpdate(WorldTimeUpdateS2CPacket packet, CallbackInfo ci) {
        if (ClientTimeDiagnostics.onClientThread()) ClientTimeDiagnostics.packet(
            MinecraftClient.getInstance().world, mc2p$beforeTimeUpdate, packet.getTime());
    }
}
