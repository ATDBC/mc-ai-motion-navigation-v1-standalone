package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.ClientBehaviorCraftGroundBridge;
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
        ClientBehaviorCraftGroundBridge.keyboardCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
    @Inject(method = "onChar", at = @At("HEAD"), cancellable = true)
    private void character(long window, int codepoint, int modifiers, CallbackInfo ci) {
        ClientBehaviorCraftGroundBridge.keyboardCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
}
