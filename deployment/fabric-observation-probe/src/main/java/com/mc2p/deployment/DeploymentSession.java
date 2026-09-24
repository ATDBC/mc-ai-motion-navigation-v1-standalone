package com.mc2p.deployment;

import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Set;

/** Credentials are consumed here and never retained in the decoded session or error messages. */
public record DeploymentSession(String episode, String observationSchemaVersion) {
    private static final String SESSION_V1 = "mc2p.deployment_session.v1";
    private static final String SESSION_V2 = "mc2p.deployment_session.v2";
    private static final String OBSERVATION_V2 = "mc2p.client_observation.v2";
    private static final String OBSERVATION_V3 = "mc2p.client_observation.v3";

    public static DeploymentSession decode(byte[] payload, String expectedToken) {
        if (payload == null || payload.length == 0 || payload.length > 16384
                || expectedToken == null || !expectedToken.matches("[0-9a-f]{64}"))
            throw new IllegalArgumentException("invalid session configuration/size");
        try {
            String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(payload)).toString();
            try (var reader = new JsonReader(new StringReader(text))) {
                reader.setLenient(false);
                var fields = new HashMap<String, String>();
                reader.beginObject();
                while (reader.hasNext()) {
                    String name = reader.nextName();
                    if (fields.containsKey(name) || reader.peek() != JsonToken.STRING)
                        throw new IllegalArgumentException("invalid session fields");
                    fields.put(name, reader.nextString());
                }
                reader.endObject();
                if (reader.peek() != JsonToken.END_DOCUMENT)
                    throw new IllegalArgumentException("invalid session authentication/schema");
                String schema = fields.get("schema_version");
                String observationSchema;
                if (SESSION_V1.equals(schema)
                        && fields.keySet().equals(Set.of("schema_version", "episode_id", "token"))) {
                    observationSchema = OBSERVATION_V2;
                } else if (SESSION_V2.equals(schema)
                        && fields.keySet().equals(Set.of("schema_version", "episode_id", "token",
                                                        "observation_schema_version"))
                        && OBSERVATION_V3.equals(fields.get("observation_schema_version"))) {
                    observationSchema = OBSERVATION_V3;
                } else {
                    throw new IllegalArgumentException("invalid session authentication/schema");
                }
                if (!fields.get("episode_id").matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
                        || !MessageDigest.isEqual(expectedToken.getBytes(StandardCharsets.US_ASCII),
                                                  fields.get("token").getBytes(StandardCharsets.UTF_8)))
                    throw new IllegalArgumentException("invalid session authentication/schema");
                return new DeploymentSession(fields.get("episode_id"), observationSchema);
            }
        } catch (IOException | IllegalStateException error) {
            // Gson error text may contain input paths; never attach raw credential-bearing input.
            throw new IllegalArgumentException("invalid session JSON");
        }
    }
}
