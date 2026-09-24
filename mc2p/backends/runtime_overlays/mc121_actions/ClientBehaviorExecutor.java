package com.mc2p.actions;

import com.google.gson.JsonNull;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mc2p.diagnostics.ClientControlDiagnostics;
import com.mc2p.observation.ClientObservationCollector;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ingame.CreativeInventoryScreen;
import net.minecraft.client.gui.screen.ingame.HandledScreen;
import net.minecraft.client.gui.screen.ingame.InventoryScreen;
import net.minecraft.screen.ScreenHandler;
import net.minecraft.screen.slot.SlotActionType;
import net.minecraft.client.input.Input;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.util.math.MathHelper;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.EntityHitResult;
import net.minecraft.util.hit.HitResult;

/** Shared normal-client behavior executor. Bridges provide only transport/tick lifecycle. */
public final class ClientBehaviorExecutor {
    private final ClientRequestGate gate = new ClientRequestGate();
    private long latestObservation = -1;
    private long observationAnchorNs;
    private ClientActionRequest lastRequest;
    private String status = "idle", reason = "none";
    private String executionThread = "none";
    private boolean onClientThread;
    private boolean dispatching;
    private boolean requestAdmitted;
    private long keyboardCallbacks, mouseCallbacks;
    private long handledScreenRenderAttempts, handledScreenRenderCompletions;
    private ClientBehaviorInput input;
    private Input previousInput;
    private ClientPlayerEntity inputPlayer;
    private final ClientMiningLoop mining = new ClientMiningLoop();
    private ClientMiningDriver miningDriver;
    private boolean miningEnabled;

    public void keyboardCallback() { if (dispatching) keyboardCallbacks++; }
    public void mouseCallback() { if (dispatching) mouseCallbacks++; }
    public void handledScreenRenderAttempt() { handledScreenRenderAttempts++; }
    public void handledScreenRenderCompletion() { handledScreenRenderCompletions++; }

    public void reset(MinecraftClient client) {
        requireClientThread(client);
        miningDriver = null;
        mining.reset(() -> {
            releaseInput();
            gate.reset();
            latestObservation = -1;
            lastRequest = null;
            status = "idle";
            reason = "none";
            keyboardCallbacks = mouseCallbacks = 0;
            handledScreenRenderAttempts = handledScreenRenderCompletions = 0;
        });
    }

    private void requireClientThread(MinecraftClient client) {
        gate.requireOwnerThread();
        if (!client.isOnThread()) throw new IllegalStateException("behavior executed off client thread");
    }

    public void tick(MinecraftClient client) {
        requireClientThread(client);
        if (input != null) input.expireOnClientTick();
        try { mining.expire(!gate.wallClockExpired(System.nanoTime())); }
        finally { if (!mining.active()) miningDriver = null; }
    }

    public void enableMining(MinecraftClient client) { requireClientThread(client); miningEnabled = true; }

    public void advance(MinecraftClient client) {
        requireClientThread(client);
        dispatching = true;
        try {
            mining.advance(gate.leaseActive(System.nanoTime()));
            if (miningEnabled && !mining.active() && client.player != null && client.world != null
                    && client.interactionManager != null && client.currentScreen == null) {
                // Vanilla's unheld path clears screen attack suppression and cancels breaking.
                // Owned input skips handleInputEvents; preserve its release behavior, not its key polling.
                ((ClientBehaviorAccess) client).mc2p$handleBlockBreaking(false);
            }
        }
        finally { dispatching = false; if (!mining.active()) miningDriver = null; }
    }

    private void stopMining() {
        miningDriver = null;
        mining.stop();
    }

    public void execute(MinecraftClient client, byte[] payload) {
        requireClientThread(client);
        requestAdmitted = false;
        ClientActionRequest action = ClientActionRequest.decode(payload);
        lastRequest = action;
        executionThread = Thread.currentThread().getName();
        onClientThread = client.isOnThread();
        long now = System.nanoTime();
        long budget = ClientRequestGate.remainingBudget(action.budgetNs(), action.observationBudgetNs(),
                                                         now - observationAnchorNs);
        String denied = action.cancel() == null
                ? gate.admit(action.episode(), action.sequence(), action.observation(), latestObservation,
                             budget, action.ticks(), now)
                : gate.validate(action.episode(), action.sequence(), action.observation(), latestObservation,
                                budget, action.ticks());
        if (denied != null) { reject(denied); return; }
        requestAdmitted = true;
        if (action.cancel() == null && input != null) input.beginSnapshot();
        if (action.cancel() == null && !action.operationKind().equals("mine_block")) stopMining();
        if (client.player == null || client.world == null || client.interactionManager == null) {
            reject("no_world"); return;
        }
        if (!client.player.isAlive()) { reject("player_dead"); return; }
        if (action.cancel() != null) {
            boolean owned = gate.cancel(action.cancel());
            if (owned && input != null) {
                input.bindAcceptedRequest(action.episode(), action.sequence());
                input.clear();
            }
            if (owned) stopMining();
            status = owned ? "cancelled" : "rejected";
            reason = owned ? "cancelled_by_request" : "not_current_owner";
            return;
        }
        boolean controls = action.forward() != 0 || action.strafe() != 0 || action.jump() || action.sneak()
                || action.sprint() || action.yawDelta() != 0 || action.pitchDelta() != 0;
        if (controls && client.currentScreen != null) { reject("screen_conflict"); return; }
        if (controls && action.operation() != null) {
            stopMining();
            reject("unsupported_control_operation_combination"); return;
        }
        if (!gate.leaseActive(System.nanoTime())) { reject("deadline_exceeded"); return; }
        dispatching = true;
        try {
            acquireInput(client);
            input.set(action.forward(), action.strafe(), action.jump(), action.sneak(), action.sprint());
            if (action.yawDelta() != 0 || action.pitchDelta() != 0) {
                client.player.setYaw(MathHelper.wrapDegrees(client.player.getYaw() + action.yawDelta()));
                client.player.setPitch(MathHelper.clamp(client.player.getPitch() + action.pitchDelta(), -90, 90));
                client.gameRenderer.updateCrosshairTarget(1.0f);
                ClientControlDiagnostics.lookApplied(
                        client, action.episode(), action.sequence(), System.nanoTime());
            }
            switch (action.operationKind()) {
                case "neutral" -> { status = "executed"; reason = controls ? "controls_applied" : "neutral"; }
                case "open_inventory" -> {
                    if (client.currentScreen instanceof InventoryScreen
                            || client.currentScreen instanceof CreativeInventoryScreen) {
                        status = "executed"; reason = "already_open";
                    } else if (client.currentScreen != null) {
                        reject("screen_conflict");
                    } else if (client.interactionManager.hasRidingInventory()) {
                        // Do not silently replace a personal-inventory request with a mount container.
                        reject("riding_inventory_conflict");
                    } else {
                        client.getTutorialManager().onInventoryOpened();
                        client.setScreen(new InventoryScreen(client.player));
                        status = "executed"; reason = "opened_locally";
                    }
                }
                case "close_screen" -> {
                    if (client.currentScreen == null) {
                        status = "executed"; reason = "already_closed";
                    } else if (client.currentScreen instanceof HandledScreen<?>) {
                        client.currentScreen.close();
                        status = "executed"; reason = "closed_normally";
                    } else {
                        reject("screen_conflict");
                    }
                }
                case "select_hotbar" -> {
                    if (client.currentScreen != null) { reject("screen_conflict"); break; }
                    client.player.getInventory().selectedSlot = action.operation().get("slot").getAsInt();
                    // The ordinary interaction manager tick performs selected-slot synchronization.
                    status = "pending_confirmation"; reason = "selected_locally";
                }
                case "click_slot" -> clickSlot(client, action.operation());
                case "interact_block" -> interactBlock(client, action.operation());
                case "mine_block" -> mineBlock(client, action.operation());
                case "attack_entity" -> attackEntity(client, action.operation());
                default -> reject("unsupported_operation");
            }
            input.bindDispatchedRequest(action.episode(), action.sequence(), status);
        } finally {
            dispatching = false;
        }
    }

    private void reject(String code) {
        status = "rejected";
        reason = code;
        if (requestAdmitted && lastRequest != null && input != null) {
            input.rejectAdmittedRequest(lastRequest.sequence());
        }
        requestAdmitted = false;
    }

    private void mineBlock(MinecraftClient client, JsonObject op) {
        if (!miningEnabled) { reject("unsupported_operation"); return; }
        var target = new ClientBlockGuard.Target(op.get("block_x").getAsInt(), op.get("block_y").getAsInt(),
                op.get("block_z").getAsInt(), op.get("face").getAsString());
        String denied = ClientMiningDriver.denial(client, target);
        if (denied == null && ((ClientBehaviorAccess) client).mc2p$attackCooldown() > 0) denied = "attack_cooldown";
        if (denied != null) { stopMining(); reject(denied); return; }
        if (miningDriver == null || !miningDriver.sameTarget(target) || !miningDriver.valid()) {
            stopMining();
            miningDriver = new ClientMiningDriver(client, target);
            mining.request(miningDriver);
        }
        status = "pending_confirmation";
        reason = "mining_requested";
    }

    private void interactBlock(MinecraftClient client, JsonObject op) {
        if (client.currentScreen != null) { reject("screen_conflict"); return; }
        if (client.player.isRiding()) { reject("riding_conflict"); return; }
        if (client.player.isUsingItem()) { reject("item_use_in_progress"); return; }
        if (client.interactionManager.isBreakingBlock()) { reject("block_break_in_progress"); return; }
        ClientBehaviorAccess access = (ClientBehaviorAccess) client;
        if (access.mc2p$itemUseCooldown() > 0) { reject("use_cooldown"); return; }
        // Refresh even for a neutral look request: other entities/world changes can occlude the old hit.
        client.gameRenderer.updateCrosshairTarget(1.0f);
        BlockHitResult hit = client.crosshairTarget instanceof BlockHitResult block
                && block.getType() == HitResult.Type.BLOCK ? block : null;
        var expected = new ClientBlockGuard.Target(op.get("block_x").getAsInt(), op.get("block_y").getAsInt(),
                op.get("block_z").getAsInt(), op.get("face").getAsString());
        var actual = hit == null ? null : new ClientBlockGuard.Target(hit.getBlockPos().getX(),
                hit.getBlockPos().getY(), hit.getBlockPos().getZ(), hit.getSide().asString());
        String denied = ClientBlockGuard.validate(expected, actual,
                hit == null ? 0 : client.player.getCameraPosVec(1.0f).distanceTo(hit.getPos()),
                client.player.getBlockInteractionRange());
        if (denied != null) { reject(denied); return; }
        if (!client.world.getWorldBorder().contains(hit.getBlockPos())) { reject("outside_world_border"); return; }
        if (!gate.leaseActive(System.nanoTime())) { reject("deadline_exceeded"); return; }
        // Vanilla selects main/off hand, normal interactBlock/item fallback, cooldown and swing.
        // A chest screen can only arrive through the server's normal handler-open packet.
        var player = client.player;
        var manager = client.interactionManager;
        boolean released = ClientUsePulse.dispatch(access::mc2p$useItem,
                () -> client.player == player && player.isUsingItem(), () -> manager.stopUsingItem(player));
        status = "pending_confirmation";
        reason = released ? "block_use_dispatched_sustained_fallback_released" : "block_use_dispatched";
    }

    private void attackEntity(MinecraftClient client, JsonObject op) {
        if (client.currentScreen != null) { reject("screen_conflict"); return; }
        if (client.player.isRiding()) { reject("riding_conflict"); return; }
        if (client.player.isUsingItem()) { reject("item_use_in_progress"); return; }
        if (client.interactionManager.isBreakingBlock()) { reject("block_break_in_progress"); return; }
        ClientBehaviorAccess access = (ClientBehaviorAccess) client;
        if (access.mc2p$attackCooldown() > 0) { reject("attack_click_cooldown"); return; }
        if (client.player.getAttackCooldownProgress(0.0f) < 1.0f) {
            reject("attack_strength_cooldown"); return;
        }
        client.gameRenderer.updateCrosshairTarget(1.0f);
        EntityHitResult hit = client.crosshairTarget instanceof EntityHitResult entity
                && entity.getType() == HitResult.Type.ENTITY ? entity : null;
        String actualTrack = hit == null ? null
                : ClientObservationCollector.currentTrackId(hit.getEntity());
        String denied = ClientEntityGuard.validate(
                op.get("entity_ref").getAsString(), actualTrack,
                hit == null ? 0.0 : client.player.getCameraPosVec(1.0f).distanceTo(hit.getPos()),
                client.player.getEntityInteractionRange());
        if (denied != null) { reject(denied); return; }
        if (!gate.leaseActive(System.nanoTime())) { reject("deadline_exceeded"); return; }
        // Vanilla returns false after a normal entity attack; this boolean is
        // the block-breaking continuation signal, not dispatch confirmation.
        access.mc2p$attack();
        status = "pending_confirmation";
        reason = "entity_attack_dispatched";
    }

    private void acquireInput(MinecraftClient client) {
        if (inputPlayer == client.player && input != null && inputPlayer.input == input) return;
        releaseInput();
        inputPlayer = client.player;
        previousInput = inputPlayer.input;
        final var world = client.world;
        input = new ClientBehaviorInput(gate, System::nanoTime, () ->
                client.player == inputPlayer && client.world == world && inputPlayer.isAlive()
                && client.currentScreen == null,
                () -> ClientObservationCollector.diagnosticSampleClock().sampledAtMonotonicNs(),
                sample -> {
                    if (sample.episodeId() != null) ClientControlDiagnostics.inputConsumed(
                            client, sample.episodeId(), sample.requestSequenceId(),
                            sample.sampledAtJvmNs(), sample.state(), sample.forward(),
                            sample.strafe(), sample.jump(), sample.sneak(), sample.sprint());
                });
        inputPlayer.input = input;
    }

    private void releaseInput() {
        if (input != null) input.clear();
        if (inputPlayer != null && inputPlayer.input == input) inputPlayer.input = previousInput;
        input = null; inputPlayer = null; previousInput = null;
    }

    private void clickSlot(MinecraftClient client, JsonObject op) {
        if (!(client.currentScreen instanceof HandledScreen<?> screen)
                || client.currentScreen instanceof CreativeInventoryScreen) {
            reject("screen_conflict"); return;
        }
        ScreenHandler handler = screen.getScreenHandler();
        if (handler != client.player.currentScreenHandler) { reject("stale_handler"); return; }
        if (client.player.isSpectator()) { reject("spectator_read_only"); return; }
        int slot = op.get("slot").getAsInt();
        boolean enabled = slot >= 0 && slot < handler.slots.size() && handler.getSlot(slot).isEnabled();
        String denied = ClientSlotGuard.validate(op.get("gui_session_id").getAsString(),
                op.get("sync_id").getAsInt(), op.get("expected_revision").getAsInt(), slot,
                ClientObservationCollector.currentGuiSessionId(client), handler.syncId,
                handler.getRevision(), handler.slots.size(), enabled);
        if (denied != null) { reject(denied); return; }
        if (!handler.canUse(client.player)) { reject("container_unavailable"); return; }
        if (!gate.leaseActive(System.nanoTime())) { reject("deadline_exceeded"); return; }
        SlotActionType type = SlotActionType.valueOf(op.get("click_type").getAsString().toUpperCase(java.util.Locale.ROOT));
        // Vanilla owns prediction, canTake/canInsert restrictions, revision and packet contents.
        // Do not mutate stacks or manufacture a ScreenHandler/ClickSlotC2SPacket here.
        client.interactionManager.clickSlot(handler.syncId, slot, op.get("button").getAsInt(), type, client.player);
        status = "pending_confirmation";
        reason = "slot_click_sent";
    }

    public JsonObject observe(MinecraftClient client, long generation) {
        requireClientThread(client);
        if (generation == 0) reset(client);
        latestObservation = generation;
        observationAnchorNs = System.nanoTime();
        if (lastRequest != null && status.equals("executed")) {
            boolean open = client.currentScreen instanceof InventoryScreen
                    || client.currentScreen instanceof CreativeInventoryScreen;
            if (lastRequest.operationKind().equals("open_inventory") && open
                    || lastRequest.operationKind().equals("close_screen") && client.currentScreen == null) {
                status = "confirmed_local";
            }
        }
        JsonObject result = new JsonObject();
        result.addProperty("schema_version", "mc2p.client_action_receipt.v3");
        result.addProperty("generation_id", generation);
        result.addProperty("execution_path", "client_behavior_v1");
        result.add("episode_id", lastRequest == null ? JsonNull.INSTANCE
                : new com.google.gson.JsonPrimitive(lastRequest.episode()));
        result.add("request_sequence_id", lastRequest == null ? JsonNull.INSTANCE
                : new com.google.gson.JsonPrimitive(lastRequest.sequence()));
        result.addProperty("status", status);
        result.addProperty("reason", reason);
        result.addProperty("execution_thread", executionThread);
        result.addProperty("on_client_thread", onClientThread);
        result.addProperty("execution_phase", "client_tick_action_boundary");
        result.addProperty("world_tick", client.world == null ? 0L : client.world.getTime());
        result.addProperty("action_keyboard_callbacks", keyboardCallbacks);
        result.addProperty("action_mouse_callbacks", mouseCallbacks);
        result.addProperty("handled_screen_render_attempts", handledScreenRenderAttempts);
        result.addProperty("handled_screen_render_completions", handledScreenRenderCompletions);
        result.addProperty("input_samples", input == null ? 0 : input.sampleCount());
        result.addProperty("leased_input_samples", input == null ? 0 : input.leasedSampleCount());
        result.addProperty("dropped_input_samples", input == null ? 0 : input.droppedSampleCount());
        result.addProperty("oldest_retained_input_tick",
                input == null ? 0 : input.oldestRetainedMovementTick());
        var applications = new JsonArray();
        for (var sample : input == null ? java.util.List.<ClientBehaviorInput.Sample>of()
                                        : input.drainSamples()) {
            var applied = new JsonObject();
            applied.addProperty("schema_version", "mc2p.input-application.v1");
            applied.addProperty("movement_tick_id", sample.movementTickId());
            if (sample.episodeId() == null || sample.requestSequenceId() < 0) {
                applied.add("episode_id", JsonNull.INSTANCE);
                applied.add("request_sequence_id", JsonNull.INSTANCE);
            } else {
                applied.addProperty("episode_id", sample.episodeId());
                applied.addProperty("request_sequence_id", sample.requestSequenceId());
            }
            applied.addProperty("sampled_at_jvm_ns", sample.sampledAtJvmNs());
            applied.addProperty("state", sample.state());
            applied.addProperty("forward", sample.forward());
            applied.addProperty("strafe", sample.strafe());
            applied.addProperty("jump", sample.jump());
            applied.addProperty("sneak", sample.sneak());
            applied.addProperty("sprint", sample.sprint());
            applications.add(applied);
        }
        result.add("input_applications", applications);
        return result;
    }
}
