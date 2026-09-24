package com.mc2p.observation;

import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Fixed field selection plus bounded air and registered-entity queries. */
public record ClientObservationRequestV3(String fieldProfile, List<Grid> airPositions,
                                         String entityTrackId) {
    public static final int MAX_AIR_POSITIONS = 512;
    public record Grid(int x, int y, int z) {}

    public ClientObservationRequestV3 {
        if (!"navigation_v1".equals(fieldProfile) && !"interaction_v1".equals(fieldProfile))
            throw new IllegalArgumentException("unsupported observation field profile");
        if (airPositions == null || airPositions.size() > MAX_AIR_POSITIONS)
            throw new IllegalArgumentException("invalid air query position budget");
        airPositions = List.copyOf(airPositions);
        var ordered = airPositions.stream().sorted(Comparator.comparingInt(Grid::x)
            .thenComparingInt(Grid::y).thenComparingInt(Grid::z)).distinct().toList();
        if (!airPositions.equals(ordered))
            throw new IllegalArgumentException("air query positions must be sorted and unique");
        if (entityTrackId != null && (entityTrackId.length() > 128
                || !entityTrackId.matches("[A-Za-z0-9][A-Za-z0-9._:/-]*")))
            throw new IllegalArgumentException("invalid entity track id");
    }

    public ClientObservationRequestV3(String fieldProfile, List<Grid> airPositions) {
        this(fieldProfile, airPositions, null);
    }
    public ClientObservationRequestV3(String fieldProfile) { this(fieldProfile, List.of(), null); }
    public static ClientObservationRequestV3 navigation() {
        return new ClientObservationRequestV3("navigation_v1");
    }
    public boolean needsTargeting() { return fieldProfile.equals("interaction_v1"); }

    private static int integer(JsonReader reader) throws IOException {
        if (reader.peek() != JsonToken.NUMBER)
            throw new IllegalArgumentException("air position coordinate must be an integer");
        String raw = reader.nextString();
        if (!raw.matches("-?(?:0|[1-9][0-9]*)"))
            throw new IllegalArgumentException("air position coordinate must be an integer");
        try { return Integer.parseInt(raw); }
        catch (NumberFormatException error) {
            throw new IllegalArgumentException("air position coordinate is out of range");
        }
    }

    private static Grid grid(JsonReader reader) throws IOException {
        reader.beginArray();
        int x = integer(reader), y = integer(reader), z = integer(reader);
        if (reader.hasNext()) throw new IllegalArgumentException("air position must contain three coordinates");
        reader.endArray();
        return new Grid(x, y, z);
    }

    public static ClientObservationRequestV3 decode(byte[] payload) {
        if (payload == null || payload.length == 0 || payload.length > 16384)
            throw new IllegalArgumentException("invalid observation request size");
        try {
            String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(payload)).toString();
            try (var reader = new JsonReader(new StringReader(text))) {
                reader.setLenient(false);
                String schema = null, profile = null, entityTrackId = null;
                List<Grid> air = List.of();
                Set<String> seen = new HashSet<>();
                reader.beginObject();
                while (reader.hasNext()) {
                    String key = reader.nextName();
                    if (!seen.add(key)) throw new IllegalArgumentException("duplicate observation request field");
                    switch (key) {
                        case "schema_version" -> {
                            if (reader.peek() != JsonToken.STRING) throw new IllegalArgumentException("invalid schema");
                            schema = reader.nextString();
                        }
                        case "field_profile" -> {
                            if (reader.peek() != JsonToken.STRING) throw new IllegalArgumentException("invalid profile");
                            profile = reader.nextString();
                        }
                        case "air_positions" -> {
                            if (reader.peek() != JsonToken.BEGIN_ARRAY) throw new IllegalArgumentException("invalid air positions");
                            var values = new ArrayList<Grid>();
                            reader.beginArray();
                            while (reader.hasNext()) {
                                if (values.size() >= MAX_AIR_POSITIONS) throw new IllegalArgumentException("air query position budget exceeded");
                                values.add(grid(reader));
                            }
                            reader.endArray();
                            air = values;
                        }
                        case "entity_track_id" -> {
                            if (reader.peek() == JsonToken.NULL) reader.nextNull();
                            else if (reader.peek() == JsonToken.STRING) entityTrackId = reader.nextString();
                            else throw new IllegalArgumentException("invalid entity track id");
                        }
                        default -> throw new IllegalArgumentException("unknown observation request field");
                    }
                }
                reader.endObject();
                if (reader.peek() != JsonToken.END_DOCUMENT
                        || !"mc2p.observation_request.v3".equals(schema) || profile == null)
                    throw new IllegalArgumentException("invalid observation request schema");
                return new ClientObservationRequestV3(profile, air, entityTrackId);
            }
        } catch (IOException | IllegalStateException error) {
            throw new IllegalArgumentException("invalid observation request JSON");
        }
    }
}
