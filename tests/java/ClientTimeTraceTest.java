import com.mc2p.diagnostics.ClientTimeTrace;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.util.ArrayList;

public final class ClientTimeTraceTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        var rows = new ArrayList<JsonObject>();
        var trace = new ClientTimeTrace(line -> rows.add(JsonParser.parseString(line).getAsJsonObject()));
        Object world = new Object();
        trace.observation(world, 0, 285);
        trace.clientTick();
        trace.worldTick(world, 285, 286);
        trace.packet(world, 286, 278, 278);
        trace.worldTick(world, 278, 279);
        trace.observation(world, 1, 279);
        trace.observation(new Object(), 0, 20);
        yes(rows.size() == 6);
        for (int i = 0; i < rows.size(); i++) yes(rows.get(i).get("event_sequence").getAsInt() == i + 1);
        yes(rows.get(4).get("client_ticks").getAsInt() == 1);
        yes(rows.get(4).get("world_ticks").getAsInt() == 2);
        yes(rows.get(2).get("packet_time").getAsInt() == 278);
        yes(rows.get(2).get("before").getAsInt() == 286);
        yes(rows.get(4).get("world_id").equals(rows.get(0).get("world_id")));
        yes(!rows.get(5).get("world_id").equals(rows.get(0).get("world_id")));
        yes(rows.get(5).get("session_id").equals(rows.get(0).get("session_id")));
        System.out.println("CLIENT_TIME_TRACE_OK");
    }
}
