import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import com.kyhsgeekcode.minecraftenv.ClientBehaviorEnvelope;

public final class ClientBehaviorEnvelopeTest {
    private static final String V2 = "mc2p.client_observation.v2";
    private static final String V3 = "mc2p.client_observation.v3";

    private static UnknownFieldSet.Field bytes(String value) {
        return UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFromUtf8(value)).build();
    }
    private static ActionSpaceMessageV2 envelope(UnknownFieldSet.Field action, UnknownFieldSet.Field request) {
        var fields = UnknownFieldSet.newBuilder().addField(50001, action);
        if (request != null) fields.addField(50004, request);
        return ActionSpaceMessageV2.newBuilder().setUnknownFields(fields.build()).build();
    }
    private static void reject(ActionSpaceMessageV2 action, boolean formal, String schema) {
        try { ClientBehaviorEnvelope.validate(action, formal, schema); }
        catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("unsafe envelope accepted");
    }
    public static void main(String[] args) {
        var action = bytes("{}");
        var navigation = bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"navigation_v1\"}");
        var interaction = bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"interaction_v1\"}");
        var validV2 = envelope(action, null);
        var validV3 = envelope(action, navigation);
        if (!ClientBehaviorEnvelope.validate(validV2, false)) throw new AssertionError("V2 formal request lost");
        if (!ClientBehaviorEnvelope.validate(validV2, true, V2)) throw new AssertionError("V2 formal request rejected");
        if (!ClientBehaviorEnvelope.validate(validV3, true, V3)) throw new AssertionError("V3 formal request rejected");
        if (!ClientBehaviorEnvelope.validate(envelope(action, interaction), true, V3))
            throw new AssertionError("interaction request rejected");
        reject(validV2, true, V3);
        reject(validV3, true, V2);
        reject(validV3.toBuilder().addCommands("fastreset ").build(), false, V3);
        reject(validV3.toBuilder().addCommands("exit").build(), true, V3);
        reject(validV3.toBuilder().setForward(true).build(), true, V3);
        reject(ActionSpaceMessageV2.newBuilder().setForward(true).build(), true, V3);
        reject(ActionSpaceMessageV2.newBuilder().addCommands("say forbidden").build(), true, V3);
        reject(ActionSpaceMessageV2.newBuilder().addCommands("fastreset give @p diamond").build(), true, V3);
        reject(envelope(action, bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"bad\"}")), true, V3);
        reject(envelope(action, bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"navigation_v1\",\"field_profile\":\"interaction_v1\"}")), true, V3);
        reject(envelope(action, bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":1}")), true, V3);
        reject(envelope(action, bytes("{\"schema_version\":\"mc2p.observation_request.v3\",\"field_profile\":\"navigation_v1\"} trailing")), true, V3);
        reject(envelope(action, UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFrom(new byte[]{(byte)0xff})).build()), true, V3);
        reject(envelope(action, UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFromUtf8("{}"))
                .addLengthDelimited(ByteString.copyFromUtf8("{}")).build()), true, V3);
        reject(envelope(UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFromUtf8("{}"))
                .addLengthDelimited(ByteString.copyFromUtf8("{}")).build(), navigation), true, V3);
        reject(envelope(action, UnknownFieldSet.Field.newBuilder().addVarint(1).build()), true, V3);
        reject(validV3, true, "mc2p.client_observation.v4");
        for (String command : new String[]{"exit", "fastreset ", "respawn"}) {
            if (ClientBehaviorEnvelope.validate(ActionSpaceMessageV2.newBuilder().addCommands(command).build(), true))
                throw new AssertionError("lifecycle reclassified as actor action");
        }
        int[] releases = {0};
        Runnable release = () -> releases[0]++;
        ClientBehaviorEnvelope.beforeDispatch(validV3, true, V3, release);
        if (releases[0] != 0) throw new AssertionError("actor action released at lifecycle gate");
        ClientBehaviorEnvelope.beforeDispatch(ActionSpaceMessageV2.newBuilder().addCommands("fastreset ").build(), true, release);
        if (releases[0] != 1) throw new AssertionError("reset did not release before command dispatch");
        try { ClientBehaviorEnvelope.beforeDispatch(validV3.toBuilder().addCommands("exit").build(), true, V3, release); }
        catch (IllegalArgumentException expected) { }
        if (releases[0] != 1) throw new AssertionError("malformed command caused side effects");
        ClientBehaviorEnvelope.beforeDispatch(ActionSpaceMessageV2.getDefaultInstance(), true, release);
        if (releases[0] != 1) throw new AssertionError("internal neutral cleared active formal session");
        if (ClientBehaviorEnvelope.validate(ActionSpaceMessageV2.newBuilder().setForward(true).build(), false))
            throw new AssertionError("explicit legacy mode lost");
        System.out.println("FORMAL_ENVELOPE_GATE_OK");
    }
}
