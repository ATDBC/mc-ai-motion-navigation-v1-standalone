package com.kyhsgeekcode.minecraftenv.mixin;

import static org.lwjgl.glfw.GLFW.GLFW_FALSE;
import static org.lwjgl.glfw.GLFW.GLFW_VISIBLE;
import static org.lwjgl.glfw.GLFW.glfwCreateWindow;
import static org.lwjgl.glfw.GLFW.glfwHideWindow;
import static org.lwjgl.glfw.GLFW.glfwWindowHint;

import com.kyhsgeekcode.minecraftenv.StructuredObservationDiagnostics;
import net.minecraft.client.util.Window;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

@Mixin(Window.class)
public class WindowOffScreenMixin {
  @Redirect(
      method = "<init>",
      at =
          @At(
              value = "INVOKE",
              target =
                  "Lorg/lwjgl/glfw/GLFW;glfwCreateWindow(IILjava/lang/CharSequence;JJ)J"))
  private long createInvisibleWindow(
      int width, int height, CharSequence title, long monitor, long share) {
    // Window's constructor resets GLFW hints before this call, so the visibility hint
    // must be applied here rather than at WindowProvider.createWindow HEAD.
    glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
    long handle = glfwCreateWindow(width, height, title, monitor, share);
    StructuredObservationDiagnostics.recordWindowCreated(handle);
    // Keep a defensive fallback immediately adjacent to creation. A run where the
    // window was visible before this hide is still rejected by the diagnostics.
    glfwHideWindow(handle);
    return handle;
  }
}
