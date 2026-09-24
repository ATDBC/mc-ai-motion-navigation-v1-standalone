package com.kyhsgeekcode.minecraftenv;

import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import com.mc2p.observation.ClientObservationRequestV3;
import java.util.Set;

/** Must run immediately after readAction, before commands, death handling or any game effect. */
public final class ClientBehaviorEnvelope {
    public static final String OBSERVATION_V2 = "mc2p.client_observation.v2";
    public static final String OBSERVATION_V3 = "mc2p.client_observation.v3";
    public static final int ACTION_FIELD_NUMBER = 50001;
    public static final int OBSERVATION_REQUEST_FIELD_NUMBER = 50004;

    private ClientBehaviorEnvelope() {}

    public static void beforeDispatch(ActionSpaceMessageV2 action, boolean formalSession, Runnable release) {
        beforeDispatch(action, formalSession, OBSERVATION_V2, release);
    }

    public static void beforeDispatch(ActionSpaceMessageV2 action, boolean formalSession,
                                      String observationSchema, Runnable release) {
        boolean formalAction = validate(action, formalSession, observationSchema);
        // Only validated lifecycle messages can release ownership before their effects begin.
        if (formalSession && !formalAction && action.getCommandsCount() == 1) release.run();
    }

    public static boolean validate(ActionSpaceMessageV2 action, boolean formalSession) {
        return validate(action, formalSession, OBSERVATION_V2);
    }

    public static boolean validate(ActionSpaceMessageV2 action, boolean formalSession, String observationSchema) {
        if (!OBSERVATION_V2.equals(observationSchema) && !OBSERVATION_V3.equals(observationSchema))
            throw new IllegalArgumentException("unsupported observation schema");
        UnknownFieldSet unknown = action.getUnknownFields();
        if (unknown.hasField(ACTION_FIELD_NUMBER)) {
            Set<Integer> expected = OBSERVATION_V3.equals(observationSchema)
                    ? Set.of(ACTION_FIELD_NUMBER, OBSERVATION_REQUEST_FIELD_NUMBER)
                    : Set.of(ACTION_FIELD_NUMBER);
            if (!action.getAllFields().isEmpty() || !unknown.asMap().keySet().equals(expected))
                throw new IllegalArgumentException("invalid formal action envelope before dispatch");
            requireSinglePayload(unknown.getField(ACTION_FIELD_NUMBER), "action");
            if (OBSERVATION_V3.equals(observationSchema)) {
                byte[] request = requireSinglePayload(
                        unknown.getField(OBSERVATION_REQUEST_FIELD_NUMBER), "observation request");
                // Decode here, before lifecycle commands or executor effects can run.
                ClientObservationRequestV3.decode(request);
            }
            return true;
        }
        if (!unknown.asMap().isEmpty())
            throw new IllegalArgumentException("unknown action envelope");
        if (formalSession && !action.getAllFields().isEmpty()) {
            boolean lifecycle = action.getCommandsCount() == 1
                    && Set.of("exit", "fastreset", "fastreset ", "respawn").contains(action.getCommands(0))
                    && action.getAllFields().keySet().stream().allMatch(field -> field.getName().equals("commands"));
            if (!lifecycle) throw new IllegalArgumentException("legacy action during formal session");
        }
        return false;
    }

    private static byte[] requireSinglePayload(UnknownFieldSet.Field field, String kind) {
        if (field.getLengthDelimitedList().size() != 1
                || field.getLengthDelimitedList().getFirst().size() > 16384
                || field.getLengthDelimitedList().getFirst().isEmpty()
                || !field.getVarintList().isEmpty() || !field.getFixed32List().isEmpty()
                || !field.getFixed64List().isEmpty() || !field.getGroupList().isEmpty())
            throw new IllegalArgumentException("invalid formal " + kind + " envelope before dispatch");
        return field.getLengthDelimitedList().getFirst().toByteArray();
    }
}
