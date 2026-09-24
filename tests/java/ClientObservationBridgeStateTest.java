import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.ClientBehaviorCraftGroundBridge;
import com.kyhsgeekcode.minecraftenv.ClientObservationCraftGroundBridge;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import java.nio.charset.StandardCharsets;
import net.minecraft.client.MinecraftClient;

public final class ClientObservationBridgeStateTest {
    private static UnknownFieldSet.Field field(String value) {
        return UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFromUtf8(value)).build();
    }
    private static ActionSpaceMessageV2 action(boolean withRequest, String profile) {
        var fields = UnknownFieldSet.newBuilder().addField(50001, field("{}"));
        if (withRequest) fields.addField(50004, field("{\"schema_version\":\"mc2p.observation_request.v3\","
                + "\"field_profile\":\"" + profile + "\"}"));
        return ActionSpaceMessageV2.newBuilder().setUnknownFields(fields.build()).build();
    }
    private static String collect(MinecraftClient client, long generation) {
        return new String(ClientObservationCraftGroundBridge.collect(client, generation), StandardCharsets.UTF_8);
    }
    public static void main(String[] args) {
        if (args.length != 1) throw new AssertionError("schema argument missing");
        System.setProperty("mc2p.observationSchema", args[0]);
        var client = new MinecraftClient(true);
        ClientBehaviorCraftGroundBridge.beginFormalSession(client, false);
        if (!ClientBehaviorCraftGroundBridge.observationSchemaVersion().equals(args[0]))
            throw new AssertionError("JVM schema was not frozen");
        if (args[0].endsWith(".v2")) {
            if (!ClientBehaviorCraftGroundBridge.apply(client, action(false, "navigation_v1")))
                throw new AssertionError("V2 formal action rejected");
            if (!collect(client, 1).equals("v2")) throw new AssertionError("V2 collector not selected");
            try { ClientBehaviorCraftGroundBridge.apply(client, action(true, "navigation_v1")); }
            catch (IllegalArgumentException expected) {
                System.out.println("CLIENT_OBSERVATION_BRIDGE_V2_OK");
                return;
            }
            throw new AssertionError("V2 accepted V3 request field");
        }
        if (!ClientBehaviorCraftGroundBridge.apply(client, action(true, "interaction_v1")))
            throw new AssertionError("V3 formal action rejected");
        try { ClientObservationCraftGroundBridge.collect(new MinecraftClient(false), 1); }
        catch (IllegalStateException expected) {
            if (!collect(client, 1).equals("v3:interaction_v1"))
                throw new AssertionError("wrong-thread collect consumed interaction profile");
            try { ClientBehaviorCraftGroundBridge.apply(new MinecraftClient(false), action(true, "navigation_v1")); }
            catch (IllegalStateException expectedApply) {
                ClientBehaviorCraftGroundBridge.apply(client, action(true, "interaction_v1"));
                if (!collect(client, 2).equals("v3:interaction_v1")) throw new AssertionError("interaction profile lost");
                if (!collect(client, 3).equals("v3:navigation_v1")) throw new AssertionError("interaction profile inherited");
                ClientBehaviorCraftGroundBridge.apply(client, action(true, "interaction_v1"));
                ClientBehaviorCraftGroundBridge.stop(client);
                if (!collect(client, 4).equals("v3:navigation_v1")) throw new AssertionError("stop retained interaction");
                System.out.println("CLIENT_OBSERVATION_BRIDGE_V3_OK");
                return;
            }
            throw new AssertionError("wrong-thread action accepted");
        }
        throw new AssertionError("wrong-thread collect accepted");
    }
}
