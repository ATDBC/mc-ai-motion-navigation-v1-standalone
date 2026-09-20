package com.mc2p.diagnostics;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.world.ClientWorld;

/** Explicit opt-in sidecar. Never replaces/clamps a clock or changes a packet/action. */
public final class ClientTimeDiagnostics {
    public static final boolean ENABLED = "1".equals(System.getenv("MC2P_TIME_DIAGNOSTICS"));
    private static final boolean SEGMENTED = "1".equals(System.getenv("MC2P_TIME_SEGMENTED"));
    private static ClientTimeSegmentWriter segments;
    private static boolean initialized, closed, shutdownHookInstalled;
    private static final ClientTimeTrace TRACE = new ClientTimeTrace(line -> {
        initialize();
        if (segments != null) { segments.accept(line); return; }
        try {
            Files.writeString(Path.of("mc2p-client-time.jsonl"), line + "\n", StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) { throw new UncheckedIOException(error); }
    });
    private ClientTimeDiagnostics() {}
    public static synchronized void initialize() {
        if (SEGMENTED && !ENABLED) throw new IllegalStateException("segmented time requires explicit time diagnostics");
        if (ClientMovementDiagnostics.ENABLED && !ENABLED) throw new IllegalStateException("movement diagnostics require explicit time diagnostics");
        if (ClientControlDiagnostics.ENABLED && !ENABLED) throw new IllegalStateException("control diagnostics require explicit time diagnostics");
        if (ClientPhysicsTickDiagnostics.ENABLED && !ENABLED) throw new IllegalStateException("physics tick diagnostics require explicit time diagnostics");
        if (initialized || closed) return;
        try {
            if (ENABLED && !shutdownHookInstalled) {
                Runtime.getRuntime().addShutdownHook(new Thread(
                        ClientTimeDiagnostics::close, "mc2p-diagnostic-close"));
                shutdownHookInstalled = true;
            }
            if (ENABLED && SEGMENTED) segments = new ClientTimeSegmentWriter(Path.of("time-events"), 16*1024*1024);
            ClientMovementDiagnostics.initialize(ENABLED);
            ClientControlDiagnostics.initialize(ENABLED);
            ClientPhysicsTickDiagnostics.initialize(ENABLED);
            initialized = true;
        } catch (IOException error) { throw new UncheckedIOException(error); }
    }
    public static synchronized void close() {
        if (closed) return;
        closed = true; // Late lifecycle callbacks must never extend an already sealed stream.
        RuntimeException primary = null;
        try { if (segments != null) segments.close(); }
        catch (IOException error) { primary = new UncheckedIOException(error); }
        finally {
            for (Runnable diagnosticClose : new Runnable[]{
                    ClientMovementDiagnostics::close, ClientControlDiagnostics::close,
                    ClientPhysicsTickDiagnostics::close}) {
                try { diagnosticClose.run(); }
                catch (RuntimeException error) {
                    if (primary == null) primary = error;
                    else primary.addSuppressed(error);
                }
            }
        }
        if (primary != null) throw primary;
    }
    public static void closeAfter(Runnable cleanup) {
        Throwable primary = null;
        try { cleanup.run(); }
        catch (RuntimeException | Error error) { primary = error; throw error; }
        finally {
            try { close(); }
            catch (RuntimeException | Error error) {
                if (primary != null) primary.addSuppressed(error);
                else throw error;
            }
        }
    }
    public static boolean onClientThread() {
        return ENABLED && !closed && MinecraftClient.getInstance().isOnThread();
    }
    static ClientTimeTrace.SampleIdentity controlIdentity() { return TRACE.sampleIdentity(); }
    public static void clientTick() {
        if (onClientThread()) TRACE.clientTick();
    }
    public static void worldTick(ClientWorld world, long before) {
        if (onClientThread()) TRACE.worldTick(world, before, world.getTime());
    }
    public static void packet(ClientWorld world, long before, long packetTime) {
        if (onClientThread()) TRACE.packet(world, before, packetTime, world.getTime());
    }
    public static void observation(MinecraftClient client, long generation) {
        if (onClientThread() && client.world != null) {
            TRACE.observation(client.world, generation, client.world.getTime());
            ClientMovementDiagnostics.observation(client,generation,TRACE.sampleIdentity());
        }
    }
}
