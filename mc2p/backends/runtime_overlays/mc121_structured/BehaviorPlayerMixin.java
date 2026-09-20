package com.kyhsgeekcode.minecraftenv.mixin;

import com.mc2p.actions.ClientBehaviorHooks;
import com.mc2p.actions.ClientBehaviorInput;
import com.mc2p.diagnostics.ClientPhysicsTickDiagnostics;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.option.KeyBinding;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Thin hook bridge; all behavior is in transport-independent ClientBehaviorHooks. */
@Mixin(ClientPlayerEntity.class)
public abstract class BehaviorPlayerMixin {
    @Inject(method = "tickMovement", at = @At("HEAD"))
    private void beforeMovement(CallbackInfo ci) {
        var player = (ClientPlayerEntity) (Object) this;
        if (player.input instanceof ClientBehaviorInput)
            ClientPhysicsTickDiagnostics.beforeMovement(MinecraftClient.getInstance());
        ClientBehaviorHooks.beforePlayerTick(player);
    }
    @Inject(method = "tickMovement", at = @At(value = "INVOKE",
            target = "Lnet/minecraft/client/input/Input;tick(ZF)V", shift = At.Shift.AFTER))
    private void afterSample(CallbackInfo ci) {
        var player = (ClientPlayerEntity) (Object) this;
        ClientBehaviorHooks.afterInputSample(player);
        if (player.input instanceof ClientBehaviorInput input) {
            var sample = input.lastSample();
            ClientPhysicsTickDiagnostics.afterInputSample(MinecraftClient.getInstance(),
                    sample.episodeId(), sample.requestSequenceId(), sample.sampledAtJvmNs(),
                    sample.state(), sample.forward(), sample.strafe(), sample.jump(),
                    sample.sneak(), sample.sprint());
        }
    }
    @Inject(method = "tickMovement", at = @At("TAIL"))
    private void afterMovement(CallbackInfo ci) {
        var player = (ClientPlayerEntity) (Object) this;
        if (player.input instanceof ClientBehaviorInput)
            ClientPhysicsTickDiagnostics.afterMovement(MinecraftClient.getInstance());
    }
    @Redirect(method = "tickMovement", at = @At(value = "INVOKE",
            target = "Lnet/minecraft/client/option/KeyBinding;isPressed()Z"))
    private boolean sprintIntent(KeyBinding binding) {
        return ClientBehaviorHooks.sprintIntent((ClientPlayerEntity) (Object) this, binding);
    }
    @Redirect(method = "tickMovement", at = @At(value = "INVOKE",
            target = "Lnet/minecraft/client/network/ClientPlayerEntity;setSprinting(Z)V"))
    private void sprintDecision(ClientPlayerEntity player, boolean value) {
        ClientBehaviorHooks.setSprinting(player, value);
    }
}
