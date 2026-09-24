package com.kyhsgeekcode.minecraftenv;

import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace;
import com.mc2p.observation.ClientObservationCollector;
import net.minecraft.client.MinecraftClient;

public final class ClientObservationCraftGroundBridge {
    public static final int FIELD_NUMBER = 50000;

    private ClientObservationCraftGroundBridge() {}

    /** Shared Kotlin hook: selects the frozen V2/V3 collector and consumes one in-flight V3 profile. */
    public static byte[] collect(MinecraftClient client, long generation) {
        if (client == null || !client.isOnThread())
            throw new IllegalStateException("observation requires client thread");
        if (generation < 0L) throw new IllegalArgumentException("invalid observation generation");
        if (ClientBehaviorEnvelope.OBSERVATION_V3.equals(
                ClientBehaviorCraftGroundBridge.observationSchemaVersion())) {
            return ClientObservationCollector.collectV3(
                    client, generation, ClientBehaviorCraftGroundBridge.takeObservationRequestV3());
        }
        return ClientObservationCollector.collect(client, generation);
    }

    public static ObservationSpace.ObservationSpaceMessage attach(
            ObservationSpace.ObservationSpaceMessage base,
            byte[] payload) {
        if (payload == null || payload.length == 0 || payload.length > 1_048_576) {
            throw new IllegalArgumentException("invalid client observation payload length");
        }
        UnknownFieldSet.Field field = UnknownFieldSet.Field.newBuilder()
                .addLengthDelimited(ByteString.copyFrom(payload))
                .build();
        UnknownFieldSet unknown = UnknownFieldSet.newBuilder(base.getUnknownFields())
                .addField(FIELD_NUMBER, field)
                .build();
        return base.toBuilder().setUnknownFields(unknown).build();
    }
}
