package com.mc2p.observation;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import net.minecraft.util.math.BlockPos;

/** Opt-in test sidecar only. Never contributes fields to an actor observation. */
public final class ClientBlockParityDiagnostics {
    public static final String FILE_NAME = "mc2p-block-parity.jsonl";
    public static final int MAX_RECORDS = 2048;
    private static final int MAX_FIRST_HITS = 1431;
    private static final int MAX_BLOCKS = 1944;
    private static final int MAX_LINE_BYTES = 262144;
    private static final Comparator<BlockPos> POSITION_ORDER = Comparator.comparingInt(BlockPos::getX)
        .thenComparingInt(BlockPos::getY).thenComparingInt(BlockPos::getZ);
    private static int records;
    private static boolean created;

    private ClientBlockParityDiagnostics() {}

    public static boolean enabled() {
        return "1".equals(System.getenv("MC2P_BLOCK_PARITY_DIAGNOSTICS"));
    }

    public static synchronized void recordIfEnabled(boolean enabled, long generationId, String fieldProfile,
            long tickBefore, Map<BlockPos,EnumSet<ClientBlockObservationV3.Source>> v3Blocks,
            Supplier<Set<BlockPos>> legacyScan, LongSupplier tickAfter) {
        if (!enabled) return;
        if (generationId < 0 || tickBefore < 0 || !("navigation_v1".equals(fieldProfile)
                || "interaction_v1".equals(fieldProfile)) || v3Blocks == null
                || legacyScan == null || tickAfter == null) {
            throw new IllegalArgumentException("invalid block parity diagnostic identity");
        }
        if (records >= MAX_RECORDS) throw new IllegalStateException("block parity diagnostic record limit exceeded");
        reserveSidecar(); // Refuse an old file before performing the diagnostic scan.

        var v3FirstHits = new HashSet<BlockPos>();
        int bodyContacts = 0;
        int currentTargets = 0;
        int multiSource = 0;
        if (v3Blocks.size() > MAX_BLOCKS) throw new IllegalStateException("block parity V3 set exceeds budget");
        for (var entry : v3Blocks.entrySet()) {
            BlockPos position = entry.getKey();
            EnumSet<ClientBlockObservationV3.Source> sources = entry.getValue();
            if (position == null || sources == null || sources.isEmpty())
                throw new IllegalStateException("invalid block parity V3 source set");
            if (sources.contains(ClientBlockObservationV3.Source.FIRST_HIT_RAY)) v3FirstHits.add(position.toImmutable());
            if (sources.contains(ClientBlockObservationV3.Source.BODY_CONTACT)) bodyContacts++;
            if (sources.contains(ClientBlockObservationV3.Source.CURRENT_TARGET)) currentTargets++;
            if (sources.size() > 1) multiSource++;
        }
        if (v3FirstHits.size() > MAX_FIRST_HITS || bodyContacts > 512 || currentTargets > 1)
            throw new IllegalStateException("block parity V3 source count exceeds budget");

        long scanStarted = System.nanoTime();
        Set<BlockPos> supplied = legacyScan.get();
        long scanCompleted = System.nanoTime();
        if (scanCompleted < scanStarted || supplied == null || supplied.size() > MAX_FIRST_HITS
                || supplied.stream().anyMatch(position -> position == null))
            throw new IllegalStateException("invalid legacy first-hit diagnostic scan");
        var legacyFirstHits = new HashSet<BlockPos>();
        supplied.forEach(position -> legacyFirstHits.add(position.toImmutable()));
        if (legacyFirstHits.size() != supplied.size())
            throw new IllegalStateException("legacy first-hit diagnostic contains duplicate coordinates");
        long completedTick = tickAfter.getAsLong();
        if (completedTick < 0) throw new IllegalStateException("block parity completion tick is invalid");

        var legacyOnly = new HashSet<>(legacyFirstHits);
        legacyOnly.removeAll(v3FirstHits);
        var v3Only = new HashSet<>(v3FirstHits);
        v3Only.removeAll(legacyFirstHits);
        if (legacyOnly.size() > MAX_FIRST_HITS || v3Only.size() > MAX_FIRST_HITS)
            throw new IllegalStateException("block parity difference exceeds budget");

        JsonObject row = new JsonObject();
        row.addProperty("schema_version", "mc2p.block-parity-diagnostic.v1");
        row.addProperty("generation_id", generationId);
        row.addProperty("field_profile", fieldProfile);
        row.addProperty("tick_before", tickBefore);
        row.addProperty("tick_after", completedTick);
        row.addProperty("legacy_first_hit_count", legacyFirstHits.size());
        row.addProperty("v3_first_hit_count", v3FirstHits.size());
        row.add("legacy_only_positions", positions(legacyOnly));
        row.add("v3_only_positions", positions(v3Only));
        row.addProperty("v3_unique_block_count", v3Blocks.size());
        row.addProperty("body_contact_count", bodyContacts);
        row.addProperty("current_target_count", currentTargets);
        row.addProperty("multi_source_block_count", multiSource);
        row.addProperty("legacy_scan_elapsed_ns", scanCompleted - scanStarted);
        byte[] encoded = (row + "\n").getBytes(StandardCharsets.UTF_8);
        if (encoded.length > MAX_LINE_BYTES) throw new IllegalStateException("block parity diagnostic line exceeds budget");
        try {
            Files.write(Path.of(FILE_NAME), encoded, StandardOpenOption.WRITE, StandardOpenOption.APPEND);
        } catch (IOException error) {
            throw new IllegalStateException("cannot append block parity diagnostic", error);
        }
        records++;
    }

    private static void reserveSidecar() {
        if (created) return;
        try {
            Files.write(Path.of(FILE_NAME), new byte[0], StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
            created = true;
        } catch (IOException error) {
            throw new IllegalStateException("cannot create fresh block parity diagnostic", error);
        }
    }

    private static JsonArray positions(Set<BlockPos> values) {
        JsonArray result = new JsonArray();
        values.stream().sorted(POSITION_ORDER).forEach(position -> {
            JsonArray coordinates = new JsonArray();
            coordinates.add(position.getX()); coordinates.add(position.getY()); coordinates.add(position.getZ());
            result.add(coordinates);
        });
        return result;
    }
}
