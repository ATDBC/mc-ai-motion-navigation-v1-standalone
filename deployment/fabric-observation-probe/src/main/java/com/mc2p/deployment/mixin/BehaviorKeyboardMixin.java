package com.mc2p.deployment.mixin;

import com.mc2p.deployment.DeploymentObservationProbe;
import com.mc2p.actions.ClientBehaviorHooks;
import net.minecraft.client.Keyboard;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(Keyboard.class)
public abstract class BehaviorKeyboardMixin {
    @Inject(method = "onKey", at = @At("HEAD"), cancellable = true)
    private void record(long window, int key, int scan, int action, int modifiers, CallbackInfo ci) {
        DeploymentObservationProbe.keyboardCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
    @Inject(method = "onChar", at = @At("HEAD"), cancellable = true)
    private void character(long window, int codepoint, int modifiers, CallbackInfo ci) {
        DeploymentObservationProbe.keyboardCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
}
