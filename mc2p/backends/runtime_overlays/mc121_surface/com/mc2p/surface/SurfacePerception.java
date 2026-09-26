package com.mc2p.surface;

import com.mc2p.observation.ClientBlockObservationV3;
import java.nio.file.Files;
import java.nio.file.Path;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientChunkEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;

/** Installs the only formal block-visibility provider. Missing native code is fatal. */
public final class SurfacePerception {
    private static SurfaceSensor sensor;
    private SurfacePerception() {}

    public static void install() {
        if (sensor != null) throw new IllegalStateException("surface perception already installed");
        String configured = System.getenv("MC2P_SURFACE_NATIVE_LIBRARY");
        if (configured == null || configured.isBlank())
            throw new IllegalStateException("surface perception native library is not configured");
        Path library = Path.of(configured).toAbsolutePath().normalize();
        if (!library.isAbsolute() || !Files.isRegularFile(library))
            throw new IllegalStateException("surface perception native library is unavailable");
        System.load(library.toString());
        sensor = new SurfaceSensor();
        ClientBlockObservationV3.installSurfaceProvider(sensor);
        ClientChunkEvents.CHUNK_LOAD.register((world, chunk) ->
                DirtyTracker.chunk(world, chunk.getPos().x, chunk.getPos().z, true));
        ClientChunkEvents.CHUNK_UNLOAD.register((world, chunk) ->
                DirtyTracker.chunk(world, chunk.getPos().x, chunk.getPos().z, false));
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> sensor.close());
    }
}
