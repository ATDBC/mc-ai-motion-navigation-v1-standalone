package com.kyhsgeekcode.minecraftenv;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.mc2p.actions.ClientBehaviorExecutor;
import com.mc2p.observation.ClientObservationRequestV3;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace;
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace;
import java.nio.charset.StandardCharsets;
import net.minecraft.client.MinecraftClient;

/** No action dictionary conversion: protobuf only carries the formal request bytes. */
public final class ClientBehaviorCraftGroundBridge {
    private static final Gson JSON = new GsonBuilder().serializeNulls().disableHtmlEscaping().create();
    private static final String OBSERVATION_SCHEMA = configuredObservationSchema();
    private static ClientBehaviorExecutor executor;
    private static boolean formalSession;
    private static ClientObservationRequestV3 observationRequest = ClientObservationRequestV3.navigation();

    private static String configuredObservationSchema() {
        String schema = System.getProperty("mc2p.observationSchema", ClientBehaviorEnvelope.OBSERVATION_V2);
        if (!ClientBehaviorEnvelope.OBSERVATION_V2.equals(schema)
                && !ClientBehaviorEnvelope.OBSERVATION_V3.equals(schema))
            throw new IllegalArgumentException("unsupported mc2p observation schema");
        return schema;
    }

    public static String observationSchemaVersion() { return OBSERVATION_SCHEMA; }

    static ClientObservationRequestV3 takeObservationRequestV3() {
        ClientObservationRequestV3 request = observationRequest;
        observationRequest = ClientObservationRequestV3.navigation();
        return request;
    }

    private static void resetObservationRequest() {
        observationRequest = ClientObservationRequestV3.navigation();
    }

    private static void requireClientThread(MinecraftClient client) {
        if (client == null || !client.isOnThread())
            throw new IllegalStateException("formal behavior requires client thread");
    }

    private static ClientBehaviorExecutor executor() {
        if (executor == null) executor = new ClientBehaviorExecutor();
        return executor;
    }

    public static void beginFormalSession(MinecraftClient client) {
        beginFormalSession(client, true);
    }

    public static void beginFormalSession(MinecraftClient client, boolean enableMining) {
        requireClientThread(client);
        formalSession = true;
        resetObservationRequest();
        if (enableMining) executor().enableMining(client);
    }

    public static void stop(MinecraftClient client) {
        requireClientThread(client);
        try {
            if (executor != null) executor.reset(client);
        } finally {
            resetObservationRequest();
        }
    }

    public static void tick(MinecraftClient client) {
        if (executor != null) executor.tick(client);
    }

    public static void advance(MinecraftClient client) {
        if (executor != null) executor.advance(client);
    }

    public static boolean apply(MinecraftClient client, ActionSpace.ActionSpaceMessageV2 action) {
        requireClientThread(client);
        if (!ClientBehaviorEnvelope.validate(action, formalSession, OBSERVATION_SCHEMA)) {
            if (formalSession) {
                // Lifecycle-only neutral messages must never enter KeyboardInfo/MouseInfo.
                if (!action.getAllFields().isEmpty()) throw new IllegalArgumentException("legacy action during formal session");
                return true;
            }
            return false; // Explicit legacy backend, before a formal session has started.
        }
        formalSession = true;
        UnknownFieldSet.Field field = action.getUnknownFields().getField(ClientBehaviorEnvelope.ACTION_FIELD_NUMBER);
        ClientObservationRequestV3 request = ClientObservationRequestV3.navigation();
        if (ClientBehaviorEnvelope.OBSERVATION_V3.equals(OBSERVATION_SCHEMA)) {
            request = ClientObservationRequestV3.decode(action.getUnknownFields()
                    .getField(ClientBehaviorEnvelope.OBSERVATION_REQUEST_FIELD_NUMBER)
                    .getLengthDelimitedList().getFirst().toByteArray());
        }
        executor().execute(client, field.getLengthDelimitedList().getFirst().toByteArray());
        observationRequest = request;
        return true;
    }

    public static void validateBeforeDispatch(MinecraftClient client, ActionSpace.ActionSpaceMessageV2 action) {
        requireClientThread(client);
        ClientBehaviorEnvelope.beforeDispatch(action, formalSession, OBSERVATION_SCHEMA, () -> {
            try { executor().reset(client); }
            finally { resetObservationRequest(); }
        });
    }

    public static ObservationSpace.ObservationSpaceMessage attach(
            MinecraftClient client, ObservationSpace.ObservationSpaceMessage observation, long generation) {
        // Formal transport ownership is JVM-lifetime sticky, including between episodes.
        byte[] receipt = JSON.toJson(executor().observe(client, generation)).getBytes(StandardCharsets.UTF_8);
        UnknownFieldSet.Field field = UnknownFieldSet.Field.newBuilder()
                .addLengthDelimited(ByteString.copyFrom(receipt)).build();
        return observation.toBuilder().setUnknownFields(UnknownFieldSet.newBuilder(observation.getUnknownFields())
                .addField(50002, field).build()).build();
    }

    public static void keyboardCallback() { if (executor != null) executor.keyboardCallback(); }
    public static void mouseCallback() { if (executor != null) executor.mouseCallback(); }
    public static void handledScreenRenderAttempt() { executor().handledScreenRenderAttempt(); }
    public static void handledScreenRenderCompletion() { executor().handledScreenRenderCompletion(); }
}
