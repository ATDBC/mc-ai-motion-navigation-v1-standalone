package com.mc2p.deployment.mixin;

import com.mc2p.deployment.DeploymentDiagnostics;
import net.minecraft.client.util.ScreenshotRecorder;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.client.texture.NativeImage;
import net.minecraft.client.gl.Framebuffer;

@Mixin(ScreenshotRecorder.class)
public abstract class ScreenshotGuardMixin {
    @Inject(method = "takeScreenshot", at = @At("HEAD"))
    private static void forbid(Framebuffer buffer, CallbackInfoReturnable<NativeImage> ci) {
        DeploymentDiagnostics.rejectCapture();
    }
}
