package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.ClientBehaviorCraftGroundBridge;
import com.mc2p.actions.ClientBehaviorHooks;
import net.minecraft.client.Mouse;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(Mouse.class)
public abstract class BehaviorMouseMixin {
    @Inject(method = "onMouseButton", at = @At("HEAD"), cancellable = true)
    private void button(long window, int button, int action, int modifiers, CallbackInfo ci) {
        ClientBehaviorCraftGroundBridge.mouseCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
    @Inject(method = "onCursorPos", at = @At("HEAD"), cancellable = true)
    private void cursor(long window, double x, double y, CallbackInfo ci) {
        ClientBehaviorCraftGroundBridge.mouseCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
    @Inject(method = "onMouseScroll", at = @At("HEAD"), cancellable = true)
    private void scroll(long window, double horizontal, double vertical, CallbackInfo ci) {
        ClientBehaviorCraftGroundBridge.mouseCallback();
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
    @Inject(method = "updateMouse", at = @At("HEAD"), cancellable = true)
    private void skipDeviceLook(double timeDelta, CallbackInfo ci) {
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
}
