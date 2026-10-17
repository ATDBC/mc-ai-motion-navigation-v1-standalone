package com.mc2p.deployment;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import com.mc2p.actions.ClientActionRequest;
import com.mc2p.observation.ClientObservationRequestV3;
import java.io.IOException;
import java.io.StringReader;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Set;

/** Strict standalone V3 step boundary. All nested fields are validated before executor effects. */
public record DeploymentStepV3(byte[] actionPayload, ClientActionRequest action,
                               ClientObservationRequestV3 observationRequest) {
    private static final int MAX_BYTES = 16_384;
    private static final Gson JSON = new GsonBuilder().serializeNulls().disableHtmlEscaping().create();

    public DeploymentStepV3 {
        if (actionPayload == null || action == null || observationRequest == null)
            throw new IllegalArgumentException("invalid deployment step");
        actionPayload = actionPayload.clone();
    }

    @Override public byte[] actionPayload() { return actionPayload.clone(); }

    public static DeploymentStepV3 decode(byte[] payload) {
        if (payload == null || payload.length == 0 || payload.length > MAX_BYTES)
            throw new IllegalArgumentException("invalid deployment step size");
        try {
            String text = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(payload)).toString();
            try (var reader = new JsonReader(new StringReader(text))) {
                reader.setLenient(false);
                JsonElement value = read(reader, 0);
                if (reader.peek() != JsonToken.END_DOCUMENT || !value.isJsonObject())
                    throw new IllegalArgumentException("invalid deployment step root");
                JsonObject root = value.getAsJsonObject();
                if (!root.keySet().equals(Set.of("schema_version", "action", "observation_request"))
                        || !strictString(root, "schema_version").equals("mc2p.client_step.v3")
                        || !root.get("action").isJsonObject()
                        || !root.get("observation_request").isJsonObject())
                    throw new IllegalArgumentException("invalid deployment step schema/fields");
                byte[] actionPayload = JSON.toJson(root.getAsJsonObject("action")).getBytes(StandardCharsets.UTF_8);
                byte[] requestPayload = JSON.toJson(root.getAsJsonObject("observation_request"))
                        .getBytes(StandardCharsets.UTF_8);
                if (actionPayload.length == 0 || actionPayload.length > MAX_BYTES
                        || requestPayload.length == 0 || requestPayload.length > MAX_BYTES)
                    throw new IllegalArgumentException("invalid deployment step component size");
                ClientActionRequest action = ClientActionRequest.decode(actionPayload);
                ClientObservationRequestV3 request = ClientObservationRequestV3.decode(requestPayload);
                return new DeploymentStepV3(actionPayload, action, request);
            }
        } catch (IOException | IllegalStateException | NumberFormatException error) {
            // Never attach raw input; a future transport must not disclose credential-adjacent frames.
            throw new IllegalArgumentException("invalid deployment step JSON");
        }
    }

    private static JsonElement read(JsonReader reader, int depth) throws IOException {
        if (depth > 5) throw new IllegalArgumentException("deployment step nesting");
        return switch (reader.peek()) {
            case BEGIN_OBJECT -> {
                JsonObject result = new JsonObject();
                reader.beginObject();
                while (reader.hasNext()) {
                    String name = reader.nextName();
                    if (result.has(name)) throw new IllegalArgumentException("duplicate deployment step key");
                    result.add(name, read(reader, depth + 1));
                }
                reader.endObject();
                yield result;
            }
            case BEGIN_ARRAY -> {
                JsonArray result = new JsonArray();
                reader.beginArray();
                while (reader.hasNext()) result.add(read(reader, depth + 1));
                reader.endArray();
                yield result;
            }
            case STRING -> new JsonPrimitive(reader.nextString());
            case NUMBER -> new JsonPrimitive(new BigDecimal(reader.nextString()));
            case BOOLEAN -> new JsonPrimitive(reader.nextBoolean());
            case NULL -> { reader.nextNull(); yield JsonNull.INSTANCE; }
            default -> throw new IllegalArgumentException("unsupported deployment step JSON value");
        };
    }

    private static String strictString(JsonObject object, String name) {
        JsonElement value = object.get(name);
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
            throw new IllegalArgumentException("invalid deployment step string");
        return value.getAsString();
    }
}
