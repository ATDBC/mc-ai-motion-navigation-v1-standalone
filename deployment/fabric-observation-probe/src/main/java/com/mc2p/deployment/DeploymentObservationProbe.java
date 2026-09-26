package com.mc2p.deployment;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mc2p.actions.ClientActionRequest;
import com.mc2p.actions.ClientBehaviorExecutor;
import com.mc2p.observation.ClientObservationCollector;
import com.mc2p.observation.ClientObservationRequestV3;
import com.mc2p.diagnostics.ClientTimeDiagnostics;
import com.mc2p.surface.SurfacePerception;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.minecraft.client.MinecraftClient;

/** Deployment-only lifecycle bridge. Shared actor logic cannot access the transport or server. */
public final class DeploymentObservationProbe implements ClientModInitializer {
    private static final Gson JSON = new GsonBuilder().serializeNulls().disableHtmlEscaping().create();
    private static ClientBehaviorExecutor executor;
    private ProbeTransport transport;
    private String token, episode, observationSchemaVersion, remoteAddress, bootstrapPhase;
    private int serverPort;
    private long generation, deadline;
    private Object world, player, connection;
    private boolean ready, pending, failed;
    private ClientObservationRequestV3 observationRequest = ClientObservationRequestV3.navigation();

    public static void keyboardCallback() { if (executor != null) executor.keyboardCallback(); }
    public static void mouseCallback() { if (executor != null) executor.mouseCallback(); }
    public static void guiAttempt() {
        DeploymentDiagnostics.guiAttempts++;
        if (executor != null) executor.handledScreenRenderAttempt();
    }
    public static void guiCompletion() {
        DeploymentDiagnostics.guiCompletions++;
        if (executor != null) executor.handledScreenRenderCompletion();
    }

    @Override public void onInitializeClient() {
        SurfacePerception.install();
        ClientTimeDiagnostics.initialize();
        token = System.getenv("MC2P_SESSION_TOKEN");
        if (token == null || !token.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("missing session credential");
        serverPort = port("MC2P_SERVER_PORT");
        transport = new ProbeTransport(port("MC2P_IPC_PORT"), 30000);
        executor = new ClientBehaviorExecutor();
        deadline = System.nanoTime() + 90_000_000_000L;
        transport.start();
        ClientTickEvents.START_CLIENT_TICK.register(this::startTick);
        ClientTickEvents.END_CLIENT_TICK.register(this::endTick);
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> {
            ClientTimeDiagnostics.closeAfter(() -> {
                transport.requestStop();
                try { executor.reset(client); }
                finally { observationRequest = ClientObservationRequestV3.navigation(); }
            });
        });
    }

    private static int port(String name) {
        String text = System.getenv(name);
        if (text == null || !text.matches("[0-9]{1,5}")) throw new IllegalArgumentException("invalid loopback port");
        int value = Integer.parseInt(text);
        if (value < 1 || value > 65535) throw new IllegalArgumentException("invalid loopback port");
        return value;
    }

    private void startTick(MinecraftClient client) {
        DeploymentDiagnostics.clientTicks++;
        if (failed) return;
        try {
            if (System.nanoTime() >= deadline) throw new IllegalStateException("session deadline exceeded");
            if (transport.failure() != null) throw new IllegalStateException("transport failed");
            if (episode == null) {
                byte[] frame = transport.poll();
                if (frame == null) { bootstrap("waiting_session"); return; }
                DeploymentSession session = DeploymentSession.decode(frame, token);
                episode = session.episode();
                observationSchemaVersion = session.observationSchemaVersion();
                observationRequest = ClientObservationRequestV3.navigation();
                token = null;
            }
            if (!ready) {
                if (client.world == null || client.player == null || client.getNetworkHandler() == null) {
                    bootstrap("waiting_world"); return;
                }
                if (client.currentScreen != null) {
                    bootstrap("waiting_screen:" + client.currentScreen.getClass().getSimpleName()); return;
                }
                if (client.player.age == 0) { bootstrap("waiting_player_tick"); return; }
                var address = client.getNetworkHandler().getConnection().getAddress();
                if (!(address instanceof InetSocketAddress remote) || !remote.getAddress().isLoopbackAddress()
                        || remote.getPort() != serverPort || client.getServer() != null)
                    throw new IllegalStateException("not the configured independent loopback server");
                remoteAddress = remote.getAddress().getHostAddress() + ":" + remote.getPort();
                world = client.world; player = client.player; connection = client.getNetworkHandler();
                ready = true; pending = true;
                executor.enableMining(client);
                bootstrap("ready");
                return;
            }
            requireWorld(client);
            executor.tick(client);
            // A coupled attack may wait until the next actual player-input sample.
            // Keep the current request exclusive until that sample has happened.
            if (pending && !executor.receiptReady()) {
                executor.advance(client);
                return;
            }
            byte[] frame = transport.poll();
            if (frame != null) {
                byte[] actionPayload;
                ClientActionRequest action;
                ClientObservationRequestV3 nextRequest = ClientObservationRequestV3.navigation();
                DeploymentStepV3 step = DeploymentStepV3.decode(frame);
                actionPayload = step.actionPayload();
                action = step.action();
                nextRequest = step.observationRequest();
                if (!action.episode().equals(episode))
                    throw new IllegalArgumentException("wrong deployment episode");
                executor.execute(client, actionPayload);
                observationRequest = nextRequest;
                generation = Math.incrementExact(generation);
                pending = true;
            }
            executor.advance(client);
        } catch (RuntimeException error) { fail(client, error); }
    }

    private void bootstrap(String phase) {
        if (!phase.equals(bootstrapPhase)) {
            bootstrapPhase = phase;
            // Read-only lifecycle diagnostics, never actor observations or screen contents.
            System.out.println("MC2P_DEPLOYMENT_BOOTSTRAP:" + phase);
        }
    }

    private void requireWorld(MinecraftClient client) {
        if (client.world != world || client.player != player || client.getNetworkHandler() != connection)
            throw new IllegalStateException("deployment world/player/connection changed");
    }

    private void endTick(MinecraftClient client) {
        if (failed || !pending) return;
        try {
            requireWorld(client);
            if (!executor.receiptReady()) return;
            ClientObservationRequestV3 request = observationRequest;
            observationRequest = ClientObservationRequestV3.navigation();
            byte[] payload = ClientObservationCollector.collectV3(client, generation, request);
            ClientTimeDiagnostics.observation(client, generation);
            var sample = new JsonObject();
            sample.addProperty("schema_version", "mc2p.deployment_sample.v2");
            sample.addProperty("episode_id", episode);
            sample.add("observation", JsonParser.parseString(new String(payload, StandardCharsets.UTF_8)));
            sample.add("receipt", executor.observe(client, generation));
            sample.add("diagnostics", DeploymentDiagnostics.sample(client, remoteAddress));
            transport.offer(JSON.toJson(sample).getBytes(StandardCharsets.UTF_8));
            pending = false;
            deadline = System.nanoTime() + 30_000_000_000L;
        } catch (RuntimeException error) { fail(client, error); }
    }

    private void fail(MinecraftClient client, RuntimeException error) {
        failed = true;
        transport.requestStop();
        // No payload, credential or potentially parser-supplied message is printed.
        System.err.println("MC2P_DEPLOYMENT_SESSION_FAILED:" + error.getClass().getSimpleName());
        try { executor.reset(client); }
        finally {
            observationRequest = ClientObservationRequestV3.navigation();
            try { client.disconnect(); }
            finally { client.scheduleStop(); }
        }
    }
}
