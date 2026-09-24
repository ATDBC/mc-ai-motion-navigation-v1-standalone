package com.mc2p.observation;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import java.nio.charset.StandardCharsets;

public final class ClientObservationJson {
    public static final int MAX_PAYLOAD_BYTES = 1_048_576;
    private static final Gson GSON = new GsonBuilder()
            .serializeNulls()
            .disableHtmlEscaping()
            .create();

    private ClientObservationJson() {}

    public static byte[] encode(JsonObject value) {
        validateFinite(value);
        byte[] result = GSON.toJson(value).getBytes(StandardCharsets.UTF_8);
        if (result.length > MAX_PAYLOAD_BYTES) {
            throw new IllegalStateException("client observation exceeds one MiB");
        }
        return result;
    }

    private static void validateFinite(JsonElement value) {
        if (value == null || value.isJsonNull()) return;
        if (value.isJsonArray()) {
            for (JsonElement child : value.getAsJsonArray()) validateFinite(child);
            return;
        }
        if (value.isJsonObject()) {
            for (var entry : value.getAsJsonObject().entrySet()) validateFinite(entry.getValue());
            return;
        }
        JsonPrimitive primitive = value.getAsJsonPrimitive();
        if (primitive.isNumber() && !Double.isFinite(primitive.getAsDouble())) {
            throw new IllegalStateException("client observation contains non-finite number");
        }
    }

    public static JsonObject object() { return new JsonObject(); }
    public static JsonArray array() { return new JsonArray(); }
}
