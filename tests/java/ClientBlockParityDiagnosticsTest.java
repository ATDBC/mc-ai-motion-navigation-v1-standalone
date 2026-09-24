import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mc2p.observation.ClientBlockObservationV3;
import com.mc2p.observation.ClientBlockParityDiagnostics;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import net.minecraft.util.math.BlockPos;

public class ClientBlockParityDiagnosticsTest {
    static void require(boolean value, String reason) { if (!value) throw new AssertionError(reason); }
    static void rejected(Runnable call) {
        try { call.run(); } catch (IllegalArgumentException | IllegalStateException expected) { return; }
        throw new AssertionError("invalid parity diagnostic was accepted");
    }
    public static void main(String[] args) throws Exception {
        Path file = Path.of(ClientBlockParityDiagnostics.FILE_NAME);
        String mode = args.length == 0 ? "normal" : args[0];
        if (mode.equals("preexisting")) {
            Files.writeString(file, "sealed", StandardOpenOption.CREATE_NEW);
            var calls = new AtomicInteger();
            rejected(() -> ClientBlockParityDiagnostics.recordIfEnabled(true, 0, "navigation_v1", 1,
                new HashMap<>(), () -> { calls.incrementAndGet(); return Set.of(); }, () -> 1));
            require(calls.get() == 0 && Files.readString(file).equals("sealed"), "old sidecar was scanned or overwritten");
            System.out.println("CLIENT_BLOCK_PARITY_PREEXISTING_OK");
            return;
        }
        if (mode.equals("limit")) {
            for (int i=0; i<ClientBlockParityDiagnostics.MAX_RECORDS; i++) {
                int tick = i;
                ClientBlockParityDiagnostics.recordIfEnabled(true, i, "navigation_v1", i,
                    new HashMap<>(), Set::of, () -> tick);
            }
            rejected(() -> ClientBlockParityDiagnostics.recordIfEnabled(true,
                ClientBlockParityDiagnostics.MAX_RECORDS, "navigation_v1", 1, new HashMap<>(), Set::of, () -> 1));
            require(Files.lines(file).count() == ClientBlockParityDiagnostics.MAX_RECORDS, "record limit changed");
            System.out.println("CLIENT_BLOCK_PARITY_LIMIT_OK");
            return;
        }

        require(!ClientBlockParityDiagnostics.enabled(), "diagnostics enabled without explicit environment opt-in");
        var disabledCalls = new AtomicInteger();
        ClientBlockParityDiagnostics.recordIfEnabled(false, 0, "navigation_v1", 1,
            new HashMap<>(), () -> { disabledCalls.incrementAndGet(); return Set.of(); }, () -> {
                disabledCalls.incrementAndGet(); return 1;
            });
        require(disabledCalls.get() == 0 && !Files.exists(file), "disabled diagnostics performed work");

        var table = new HashMap<BlockPos,EnumSet<ClientBlockObservationV3.Source>>();
        var firstOnly = new BlockPos(1, 64, 0);
        var firstOnlyEarlier = new BlockPos(0, 65, 0);
        var contactOnly = new BlockPos(0, 63, 0);
        var targetOnly = new BlockPos(0, 64, 2);
        var firstAndContact = new BlockPos(-1, 64, 0);
        ClientBlockObservationV3.authorize(table, firstOnly, ClientBlockObservationV3.Source.FIRST_HIT_RAY);
        ClientBlockObservationV3.authorize(table, firstOnlyEarlier, ClientBlockObservationV3.Source.FIRST_HIT_RAY);
        ClientBlockObservationV3.authorize(table, contactOnly, ClientBlockObservationV3.Source.BODY_CONTACT);
        ClientBlockObservationV3.authorize(table, targetOnly, ClientBlockObservationV3.Source.CURRENT_TARGET);
        ClientBlockObservationV3.authorize(table, firstAndContact, ClientBlockObservationV3.Source.FIRST_HIT_RAY);
        ClientBlockObservationV3.authorize(table, firstAndContact, ClientBlockObservationV3.Source.BODY_CONTACT);
        var legacyOnly = new BlockPos(2, 64, 0);
        var legacyOnlyEarlier = new BlockPos(-2, 64, 0);
        var calls = new AtomicInteger();
        ClientBlockParityDiagnostics.recordIfEnabled(true, 7, "interaction_v1", 100, table, () -> {
            calls.incrementAndGet();
            return new LinkedHashSet<>(java.util.List.of(legacyOnly, firstAndContact, legacyOnlyEarlier));
        }, () -> { require(calls.get()==1, "tick_after sampled before legacy scan"); return 100; });
        require(calls.get() == 1, "enabled diagnostic did not perform exactly one legacy scan");
        String line = Files.readString(file).trim();
        require(!line.contains("block_rays") && !line.contains("pov"), "legacy or image payload leaked");
        JsonObject value = JsonParser.parseString(line).getAsJsonObject();
        require(value.keySet().equals(Set.of("schema_version","generation_id","field_profile","tick_before","tick_after",
            "legacy_first_hit_count","v3_first_hit_count","legacy_only_positions","v3_only_positions",
            "v3_unique_block_count","body_contact_count","current_target_count","multi_source_block_count",
            "legacy_scan_elapsed_ns")), "diagnostic schema changed");
        require(value.get("generation_id").getAsLong()==7 && value.get("field_profile").getAsString().equals("interaction_v1"), "identity missing");
        require(value.get("legacy_first_hit_count").getAsInt()==3 && value.get("v3_first_hit_count").getAsInt()==3,
            "first-hit counts include non-ray sources");
        require(value.get("v3_unique_block_count").getAsInt()==5 && value.get("body_contact_count").getAsInt()==2
            && value.get("current_target_count").getAsInt()==1 && value.get("multi_source_block_count").getAsInt()==1,
            "source counts changed");
        require(position(value.getAsJsonArray("legacy_only_positions"),0).equals(legacyOnlyEarlier)
            && position(value.getAsJsonArray("legacy_only_positions"),1).equals(legacyOnly), "legacy diff order wrong");
        require(position(value.getAsJsonArray("v3_only_positions"),0).equals(firstOnlyEarlier)
            && position(value.getAsJsonArray("v3_only_positions"),1).equals(firstOnly), "V3 diff order wrong");
        require(value.get("legacy_scan_elapsed_ns").getAsLong()>=0, "scan time invalid");

        var tooLarge = new HashMap<BlockPos,EnumSet<ClientBlockObservationV3.Source>>();
        for (int i=0; i<1432; i++)
            tooLarge.put(new BlockPos(i,0,0), EnumSet.of(ClientBlockObservationV3.Source.FIRST_HIT_RAY));
        rejected(() -> ClientBlockParityDiagnostics.recordIfEnabled(true, 8, "navigation_v1", 101,
            tooLarge, Set::of, () -> 101));
        System.out.println("CLIENT_BLOCK_PARITY_DIAGNOSTICS_OK");
    }
    static BlockPos position(JsonArray values, int index) {
        JsonArray value = values.get(index).getAsJsonArray();
        return new BlockPos(value.get(0).getAsInt(),value.get(1).getAsInt(),value.get(2).getAsInt());
    }
}
