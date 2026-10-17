package com.mc2p.deployment.mixin;

import com.mc2p.actions.ClientBehaviorHooks;
import com.mc2p.actions.ClientBehaviorAccess;
import net.minecraft.client.MinecraftClient;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.gen.Accessor;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(MinecraftClient.class)
public abstract class BehaviorInputMixin implements ClientBehaviorAccess {
    @Accessor("itemUseCooldown")
    public abstract int mc2p$itemUseCooldown();

    @Invoker("doItemUse")
    public abstract void mc2p$useItem();

    @Accessor("attackCooldown")
    public abstract int mc2p$attackCooldown();

    @Invoker("doAttack")
    public abstract boolean mc2p$attack();

    @Invoker("handleBlockBreaking")
    public abstract void mc2p$handleBlockBreaking(boolean held);

    @Inject(method = "handleInputEvents", at = @At("HEAD"), cancellable = true)
    private void skipDevices(CallbackInfo ci) {
        if (ClientBehaviorHooks.ownsDeviceInput()) ci.cancel();
    }
}
