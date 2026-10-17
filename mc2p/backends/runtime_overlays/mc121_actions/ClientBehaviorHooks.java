package com.mc2p.actions;

import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.option.KeyBinding;

/** Normal-client hook semantics, shared across transport adapters. No generated device events. */
public final class ClientBehaviorHooks {
    private ClientBehaviorHooks() {}
    public static boolean ownsDeviceInput() {
        var client = MinecraftClient.getInstance();
        return client.player != null && client.player.input instanceof ClientBehaviorInput;
    }
    public static boolean sprintIntent(ClientPlayerEntity player, KeyBinding binding) {
        if (player.input instanceof ClientBehaviorInput input
                && binding == MinecraftClient.getInstance().options.sprintKey) return input.sprintRequested();
        return binding.isPressed();
    }
    public static void afterInputSample(ClientPlayerEntity player) {
        if (player.input instanceof ClientBehaviorInput input && !input.sprintRequested()) player.setSprinting(false);
    }
    public static void beforePlayerTick(ClientPlayerEntity player) {
        if (player.input instanceof ClientBehaviorInput input) input.prepareForPlayerTick();
    }
    public static void setSprinting(ClientPlayerEntity player, boolean vanillaDecision) {
        if (player.input instanceof ClientBehaviorInput input) vanillaDecision &= input.sprintRequested();
        player.setSprinting(vanillaDecision);
    }
}
