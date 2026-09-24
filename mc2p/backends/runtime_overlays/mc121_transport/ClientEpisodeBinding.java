package com.kyhsgeekcode.minecraftenv;

import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace.ObservationSpaceMessage;
import java.util.UUID;

/** Adapter-only episode binding; never part of the actor's observation or action contract. */
public final class ClientEpisodeBinding {
    public static final int FIELD_NUMBER = 50003;
    private final Thread owner = Thread.currentThread();
    private final String clientId = UUID.randomUUID().toString();
    private final String observationSchema;
    private long generation;
    private ByteString epoch;

    /** Production construction reads the reset-frozen JVM property; explicit construction keeps harnesses deterministic. */
    public ClientEpisodeBinding() {
        this(System.getProperty("mc2p.observationSchema", ClientBehaviorEnvelope.OBSERVATION_V2));
    }

    public ClientEpisodeBinding(String observationSchema) {
        if (!ClientBehaviorEnvelope.OBSERVATION_V2.equals(observationSchema)
                && !ClientBehaviorEnvelope.OBSERVATION_V3.equals(observationSchema))
            throw new IllegalArgumentException("unsupported observation schema");
        this.observationSchema = observationSchema;
    }

    private void requireOwner() {
        if (Thread.currentThread() != owner) throw new IllegalStateException("episode binding outside owner thread");
    }

    public void invalidate() {
        requireOwner();
        epoch = null;
    }

    /** Called only when a real new episode is ready to publish its reset observation. */
    public void beginEpisode() {
        requireOwner();
        if (epoch != null) throw new IllegalStateException("episode binding is already active");
        generation = Math.incrementExact(generation);
        epoch = ByteString.copyFromUtf8("mc2p.transport-epoch.v1/" + clientId + "/" + generation);
    }

    private void requireActive() {
        requireOwner();
        if (epoch == null) throw new IllegalStateException("episode binding unavailable during reset");
    }

    public ObservationSpaceMessage attach(ObservationSpaceMessage observation) {
        requireActive();
        if (observation.getUnknownFields().hasField(FIELD_NUMBER))
            throw new IllegalArgumentException("observation already has a transport epoch");
        var field = UnknownFieldSet.Field.newBuilder().addLengthDelimited(epoch).build();
        return observation.toBuilder().setUnknownFields(UnknownFieldSet.newBuilder(observation.getUnknownFields())
                .addField(FIELD_NUMBER, field).build()).build();
    }

    /** Verify before any lifecycle or actor effect; the existing strict action envelope remains intact. */
    public ActionSpaceMessageV2 unwrap(ActionSpaceMessageV2 action) {
        requireActive();
        var field = action.getUnknownFields().getField(FIELD_NUMBER);
        if (field.getLengthDelimitedList().size() != 1 || !field.getLengthDelimitedList().getFirst().equals(epoch)
                || !field.getVarintList().isEmpty() || !field.getFixed32List().isEmpty()
                || !field.getFixed64List().isEmpty() || !field.getGroupList().isEmpty())
            throw new IllegalArgumentException("invalid or stale transport epoch");
        var unwrapped = action.toBuilder().setUnknownFields(UnknownFieldSet.newBuilder(action.getUnknownFields())
                .clearField(FIELD_NUMBER).build()).build();
        ClientBehaviorEnvelope.validate(unwrapped, true, observationSchema);
        return unwrapped;
    }
}
