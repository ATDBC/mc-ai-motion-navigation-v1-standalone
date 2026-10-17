package com.mc2p.deployment.mixin;

import com.mc2p.deployment.DeploymentObservationProbe;
import net.minecraft.client.gui.DrawContext;
import net.minecraft.client.gui.screen.Screen;
import net.minecraft.client.gui.screen.ingame.HandledScreen;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Installed only by structured_only. Screen initialization/tick/handler logic is untouched. */
@Mixin(Screen.class)
public abstract class HandledScreenRenderMixin {
    @Inject(method = "renderWithTooltip", at = @At("HEAD"), cancellable = true)
    private void skipHandledDrawing(DrawContext context, int x, int y, float delta, CallbackInfo ci) {
        if ((Object) this instanceof HandledScreen<?>) {
            DeploymentObservationProbe.guiAttempt();
            ci.cancel();
        }
    }

    @Inject(method = "renderWithTooltip", at = @At("TAIL"))
    private void recordCompletion(DrawContext context, int x, int y, float delta, CallbackInfo ci) {
        if ((Object) this instanceof HandledScreen<?>)
            DeploymentObservationProbe.guiCompletion();
    }
}
