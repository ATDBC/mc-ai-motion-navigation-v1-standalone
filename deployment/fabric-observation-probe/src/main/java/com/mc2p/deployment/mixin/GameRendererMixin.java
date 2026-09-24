package com.mc2p.deployment.mixin;

import com.mc2p.deployment.DeploymentDiagnostics;
import com.mc2p.observation.ClientCrosshairAccess;
import net.minecraft.client.render.GameRenderer;
import net.minecraft.client.render.RenderTickCounter;
import net.minecraft.entity.Entity;
import net.minecraft.util.hit.HitResult;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Invoker;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(GameRenderer.class)
public abstract class GameRendererMixin implements ClientCrosshairAccess {
  @Override
  @Invoker("findCrosshairTarget")
  public abstract HitResult mc2p$findCrosshairTarget(
      Entity cameraEntity, double blockRange, double entityRange, float tickDelta);

  @Inject(method = "renderWorld", at = @At("HEAD"), cancellable = true)
  private void skipRenderWorld(RenderTickCounter tickCounter, CallbackInfo callback) {
    DeploymentDiagnostics.worldAttempts++;
    callback.cancel();
  }

  @Inject(method = "renderWorld", at = @At("TAIL"))
  private void recordRenderWorldCompletion(
      RenderTickCounter tickCounter, CallbackInfo callback) {
    DeploymentDiagnostics.worldCompletions++;
  }
}
