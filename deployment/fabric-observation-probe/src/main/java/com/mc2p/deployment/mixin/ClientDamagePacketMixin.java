package com.mc2p.deployment.mixin;

import com.mc2p.observation.ClientObservationCollector;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayNetworkHandler;
import net.minecraft.network.packet.s2c.play.EntityDamageS2CPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ClientPlayNetworkHandler.class)
public abstract class ClientDamagePacketMixin {
    @Inject(method = "onEntityDamage", at = @At("HEAD"))
    private void mc2p$recordDamage(EntityDamageS2CPacket packet, CallbackInfo ci) {
        MinecraftClient client = MinecraftClient.getInstance();
        // The Netty invocation exits through forceMainThread. Only the later
        // client-thread application becomes a formal observation fact.
        if (client.isOnThread()) {
            ClientObservationCollector.recordDamage(client, packet);
        }
    }
}
