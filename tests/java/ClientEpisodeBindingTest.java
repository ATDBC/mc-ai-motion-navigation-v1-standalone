import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.ClientEpisodeBinding;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace.ObservationSpaceMessage;

public final class ClientEpisodeBindingTest {
    static final String V2 = "mc2p.client_observation.v2";
    static final String V3 = "mc2p.client_observation.v3";
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    static UnknownFieldSet.Field field(ByteString token) {
        return UnknownFieldSet.Field.newBuilder().addLengthDelimited(token).build();
    }
    static ActionSpaceMessageV2 bound(ByteString token) {
        return ActionSpaceMessageV2.newBuilder().setUnknownFields(UnknownFieldSet.newBuilder()
                .addField(50001, field(ByteString.copyFromUtf8("{}")))
                .addField(50003, field(token)).build()).build();
    }
    static ActionSpaceMessageV2 boundV3(ByteString token, String profile) {
        return ActionSpaceMessageV2.newBuilder().setUnknownFields(UnknownFieldSet.newBuilder()
                .addField(50001, field(ByteString.copyFromUtf8("{}")))
                .addField(50003, field(token))
                .addField(50004, field(ByteString.copyFromUtf8("{\"schema_version\":\"mc2p.observation_request.v3\","
                        + "\"field_profile\":\"" + profile + "\"}"))).build()).build();
    }
    static void reject(ClientEpisodeBinding binding, ActionSpaceMessageV2 action) {
        try { binding.unwrap(action); throw new AssertionError("stale/malformed action accepted"); }
        catch (IllegalArgumentException | IllegalStateException expected) {}
    }
    public static void main(String[] args) throws Exception {
        var binding = new ClientEpisodeBinding(V2);
        reject(binding, ActionSpaceMessageV2.getDefaultInstance());
        binding.beginEpisode();
        var first = binding.attach(ObservationSpaceMessage.getDefaultInstance());
        ByteString oldToken = first.getUnknownFields().getField(50003).getLengthDelimitedList().getFirst();
        var oldAction = bound(oldToken);
        check(binding.unwrap(oldAction).getUnknownFields().asMap().keySet().equals(java.util.Set.of(50001)));
        reject(binding, ActionSpaceMessageV2.getDefaultInstance());
        reject(binding, oldAction.toBuilder().setForward(true).build());
        reject(binding, oldAction.toBuilder().addCommands("fastreset ").build());
        try { binding.attach(first); throw new AssertionError("binding silently overwritten"); }
        catch (IllegalArgumentException expected) {}
        try { binding.beginEpisode(); throw new AssertionError("active episode silently replaced"); }
        catch (IllegalStateException expected) {}
        binding.invalidate();
        reject(binding, oldAction); // Request arrives DURING reset.
        binding.beginEpisode();
        reject(binding, oldAction); // Request arrives FIRST after new reset observation 0.
        var second = binding.attach(ObservationSpaceMessage.getDefaultInstance());
        ByteString token = second.getUnknownFields().getField(50003).getLengthDelimitedList().getFirst();
        check(!token.equals(oldToken));
        check(binding.unwrap(bound(token)).getUnknownFields().hasField(50001));
        for (UnknownFieldSet.Field bad : new UnknownFieldSet.Field[]{
                field(ByteString.EMPTY), field(ByteString.copyFromUtf8("unknown/v9")),
                UnknownFieldSet.Field.newBuilder().addVarint(1).build(),
                UnknownFieldSet.Field.newBuilder(field(token)).addVarint(1).build(),
                UnknownFieldSet.Field.newBuilder(field(token)).addLengthDelimited(token).build()}) {
            reject(binding, bound(token).toBuilder().setUnknownFields(UnknownFieldSet.newBuilder()
                    .addField(50003, bad).build()).build());
        }
        // Bound lifecycle remains a lifecycle message, never a formal action with commands mixed in.
        var reset = ActionSpaceMessageV2.newBuilder().addCommands("fastreset ").setUnknownFields(
                UnknownFieldSet.newBuilder().addField(50003, field(token)).build()).build();
        check(binding.unwrap(reset).getCommands(0).equals("fastreset "));
        reject(binding, reset.toBuilder().setCommands(0, "give @p diamond").build());
        var neutral = reset.toBuilder().clearCommands().build();
        check(binding.unwrap(neutral).equals(ActionSpaceMessageV2.getDefaultInstance()));
        Throwable[] error = {null};
        Thread other = new Thread(() -> { try { binding.invalidate(); } catch (Throwable failed) { error[0] = failed; } });
        other.start(); other.join(1000);
        check(!other.isAlive() && error[0] instanceof IllegalStateException);
        check(binding.unwrap(bound(token)).getUnknownFields().hasField(50001));
        var fresh = new ClientEpisodeBinding(V2); fresh.beginEpisode();
        reject(fresh, bound(token)); // A different client instance cannot reuse an epoch.
        var v3 = new ClientEpisodeBinding(V3);
        v3.beginEpisode();
        ByteString v3Token = v3.attach(ObservationSpaceMessage.getDefaultInstance())
                .getUnknownFields().getField(50003).getLengthDelimitedList().getFirst();
        var v3Unwrapped = v3.unwrap(boundV3(v3Token, "interaction_v1"));
        check(v3Unwrapped.getUnknownFields().asMap().keySet().equals(java.util.Set.of(50001, 50004)));
        reject(v3, bound(v3Token));
        reject(v3, boundV3(v3Token, "invalid"));
        v3.invalidate();
        reject(v3, boundV3(v3Token, "navigation_v1"));
        try { new ClientEpisodeBinding("mc2p.client_observation.v4"); throw new AssertionError(); }
        catch (IllegalArgumentException expected) {}
        System.out.println("OBSERVATION_WIRE=" + java.util.HexFormat.of().formatHex(second.toByteArray()));
        System.out.println("ACTION_WIRE=" + java.util.HexFormat.of().formatHex(bound(token).toByteArray()));
        System.out.println("CLIENT_EPISODE_BINDING_OK");
    }
}
