package com.mc2p.deployment.mixin;

import com.mc2p.deployment.DeploymentDiagnostics;
import net.minecraft.client.texture.NativeImage;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(NativeImage.class)
public abstract class NativeImageGuardMixin {
    @Inject(method = {"loadFromTextureImage", "readDepthComponent"}, at = @At("HEAD"))
    private void forbidReadback(CallbackInfo ci) { DeploymentDiagnostics.rejectCapture(); }

    @Inject(method = {"writeTo(Ljava/io/File;)V", "writeTo(Ljava/nio/file/Path;)V"}, at = @At("HEAD"))
    private void forbidFileEncoding(CallbackInfo ci) { DeploymentDiagnostics.rejectEncoding(); }

    @Inject(method = "getBytes", at = @At("HEAD"))
    private void forbidByteEncoding(CallbackInfoReturnable<byte[]> ci) { DeploymentDiagnostics.rejectEncoding(); }
}
