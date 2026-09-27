package com.mc2p.deployment;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonElement;
import java.nio.charset.StandardCharsets;

/** Builds one transport envelope while preserving collector-owned observation bytes. */
public final class DeploymentSampleEncoder {
    private static final Gson JSON = new GsonBuilder()
            .serializeNulls().disableHtmlEscaping().create();
    private static final byte[] OBSERVATION_PREFIX =
            "{\"schema_version\":\"mc2p.deployment_sample.v2\",\"episode_id\":"
                    .getBytes(StandardCharsets.UTF_8);

    private DeploymentSampleEncoder() {}

    public static byte[] encode(String episode, byte[] observation,
                                JsonElement receipt, JsonElement diagnostics) {
        if (episode == null || episode.isEmpty() || observation == null
                || observation.length < 2 || observation[0] != '{'
                || observation[observation.length - 1] != '}'
                || receipt == null || diagnostics == null) {
            throw new IllegalArgumentException("invalid deployment sample component");
        }
        byte[] episodeJson = JSON.toJson(episode).getBytes(StandardCharsets.UTF_8);
        byte[] middle = ",\"observation\":".getBytes(StandardCharsets.UTF_8);
        byte[] suffix = (",\"receipt\":" + JSON.toJson(receipt)
                + ",\"diagnostics\":" + JSON.toJson(diagnostics) + "}")
                .getBytes(StandardCharsets.UTF_8);
        byte[] result = new byte[OBSERVATION_PREFIX.length + episodeJson.length
                + middle.length + observation.length + suffix.length];
        int offset = 0;
        offset = copy(OBSERVATION_PREFIX, result, offset);
        offset = copy(episodeJson, result, offset);
        offset = copy(middle, result, offset);
        offset = copy(observation, result, offset);
        copy(suffix, result, offset);
        return result;
    }

    private static int copy(byte[] source, byte[] target, int offset) {
        System.arraycopy(source, 0, target, offset, source.length);
        return offset + source.length;
    }
}
