package com.mc2p.actions;

import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.network.ClientPlayerInteractionManager;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;

/** Normal vanilla mining at a currently raycast-visible target. No direct block or packet writes. */
public final class ClientMiningDriver implements ClientMiningLoop.Driver {
    private final MinecraftClient client;
    private final ClientPlayerEntity player;
    private final ClientWorld world;
    private final ClientPlayerInteractionManager manager;
    private final ClientBlockGuard.Target target;
    private final BlockState initialState;

    public ClientMiningDriver(MinecraftClient client, ClientBlockGuard.Target target) {
        this.client = client;
        player = client.player; world = client.world; manager = client.interactionManager;
        this.target = target;
        // Caller has validated an actual raycast hit immediately before construction.
        initialState = world.getBlockState(((BlockHitResult) client.crosshairTarget).getBlockPos());
    }

    public boolean sameTarget(ClientBlockGuard.Target candidate) { return target.equals(candidate); }

    public static String denial(MinecraftClient client, ClientBlockGuard.Target expected) {
        if (client.player == null || client.world == null || client.interactionManager == null) return "no_world";
        if (!client.player.isAlive()) return "player_dead";
        if (client.currentScreen != null) return "screen_conflict";
        if (client.player.isRiding()) return "riding_conflict";
        if (client.player.isUsingItem()) return "item_use_in_progress";
        if (client.player.isSpectator()) return "spectator_read_only";
        if (!client.player.getMainHandStack().isItemEnabled(client.world.getEnabledFeatures())) return "item_disabled";
        client.gameRenderer.updateCrosshairTarget(1.0f);
        BlockHitResult hit = client.crosshairTarget instanceof BlockHitResult block
                && block.getType() == HitResult.Type.BLOCK ? block : null;
        var actual = hit == null ? null : new ClientBlockGuard.Target(hit.getBlockPos().getX(),
                hit.getBlockPos().getY(), hit.getBlockPos().getZ(), hit.getSide().asString());
        String denied = ClientBlockGuard.validate(expected, actual,
                hit == null ? 0 : client.player.getCameraPosVec(1.0f).distanceTo(hit.getPos()),
                client.player.getBlockInteractionRange());
        if (denied != null) return denied;
        if (!client.world.getWorldBorder().contains(hit.getBlockPos())) return "outside_world_border";
        return client.world.getBlockState(hit.getBlockPos()).isAir() ? "target_unavailable" : null;
    }

    private boolean sameContext() {
        return client.player == player && client.world == world && client.interactionManager == manager;
    }

    @Override public boolean valid() {
        if (!sameContext() || denial(client, target) != null) return false;
        return world.getBlockState(((BlockHitResult) client.crosshairTarget).getBlockPos()) == initialState;
    }

    @Override public void start() { ((ClientBehaviorAccess) client).mc2p$attack(); }
    @Override public void progress() { ((ClientBehaviorAccess) client).mc2p$handleBlockBreaking(true); }

    @Override public void cancel() {
        // A manager from a replaced world refers to the shared client; do not let it touch the new player.
        if (sameContext()) manager.cancelBlockBreaking();
    }
}
