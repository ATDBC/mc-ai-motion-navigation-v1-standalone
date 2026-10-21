package com.mc2p.actions;

import com.google.gson.*;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Set;

/** Strict bounded decoder. No arbitrary Java calls, commands, keys, or coercions. */
public record ClientActionRequest(String episode, long sequence, long observation,
        long budgetNs, long observationBudgetNs, int ticks, int forward, int strafe, boolean jump, boolean sneak,
        boolean sprint, float yawDelta, float pitchDelta, JsonObject operation, Long cancel,
        FirstEffectWindow firstEffectWindow, SafeDefault safeDefault) {

    public record Movement(int forward, int strafe, boolean jump, boolean sneak, boolean sprint) {}
    public record FirstEffectWindow(long earliestTick, long latestTick) {}
    public record SafeDefault(long activationTick, Movement movement, int validForTicks, long wallBudgetNs) {}

    public String operationKind() {
        return operation == null ? "neutral" : operation.get("kind").getAsString();
    }

    public long requiredSafeDefaultBudgetNs(long nextMovementTick) {
        if (nextMovementTick < 0) throw new IllegalArgumentException("invalid next movement tick");
        if (safeDefault == null) return 0;
        long wait = Math.max(0L, safeDefault.activationTick - nextMovementTick);
        if (wait > (Long.MAX_VALUE - safeDefault.wallBudgetNs) / 50_000_000L)
            return Long.MAX_VALUE;
        return wait * 50_000_000L + safeDefault.wallBudgetNs;
    }

    public String windowedActivationDenial() {
        if (firstEffectWindow != null && operation != null) return "windowed_operation_unsupported";
        return null;
    }

    public String timingAdmissionDenial(long nextMovementTick) {
        String windowed = windowedActivationDenial();
        if (windowed != null) return windowed;
        if (nextMovementTick < 0) return "invalid_movement_tick";
        if (safeDefault == null) return null;
        long base = nextMovementTick;
        if (firstEffectWindow != null) {
            if (firstEffectWindow.earliestTick != firstEffectWindow.latestTick)
                return "safe_default_requires_exact_window";
            base = firstEffectWindow.earliestTick;
        }
        if (base > Long.MAX_VALUE - ticks
                || safeDefault.activationTick != base + ticks)
            return "safe_default_activation_mismatch";
        return null;
    }

    public static ClientActionRequest decode(byte[] bytes) {
        return decode(bytes, "mc2p.client_action.v1");
    }

    public static ClientActionRequest decode(byte[] bytes, String negotiatedSchema) {
        if (bytes == null || bytes.length == 0 || bytes.length > 16384)
            throw new IllegalArgumentException("action size");
        try {
            String text = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes)).toString();
            try (JsonReader reader = new JsonReader(new StringReader(text))) {
                reader.setLenient(false);
                JsonElement value = read(reader, 0);
                if (reader.peek() != JsonToken.END_DOCUMENT || !value.isJsonObject())
                    throw new IllegalArgumentException("action root");
                JsonObject root = value.getAsJsonObject();
                if (!Set.of("mc2p.client_action.v1", "mc2p.client_action.v2").contains(negotiatedSchema))
                    throw new IllegalArgumentException("negotiated action schema");
                String schema = string(root, "schema_version");
                if (!schema.equals(negotiatedSchema))
                    throw new IllegalArgumentException("action schema");
                if (schema.equals("mc2p.client_action.v1")) {
                    keys(root, "schema_version", "episode_id", "request_sequence_id",
                            "observation_sequence_id", "remaining_budget_ns", "valid_for_ticks",
                            "observation_budget_ns", "movement", "look", "operation",
                            "cancel_request_sequence_id");
                } else {
                    keys(root, "schema_version", "episode_id", "request_sequence_id",
                            "observation_sequence_id", "remaining_budget_ns", "valid_for_ticks",
                            "observation_budget_ns", "movement", "look", "operation",
                            "cancel_request_sequence_id", "first_effect_window", "safe_default");
                }
                String episode = string(root, "episode_id");
                if (!episode.matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"))
                    throw new IllegalArgumentException("episode");
                long sequence = integer(root, "request_sequence_id", 0, Long.MAX_VALUE);
                long observation = integer(root, "observation_sequence_id", 0, Long.MAX_VALUE);
                long budget = integer(root, "remaining_budget_ns", 1, 30_000_000_000L);
                long observationBudget = integer(root, "observation_budget_ns", 1, Long.MAX_VALUE);
                int ticks = (int) integer(root, "valid_for_ticks", 1, 20);
                JsonObject move = object(root, "movement");
                keys(move, "forward", "strafe", "jump", "sneak", "sprint");
                int forward = (int) integer(move, "forward", -1, 1);
                int strafe = (int) integer(move, "strafe", -1, 1);
                boolean jump = bool(move, "jump"), sneak = bool(move, "sneak"), sprint = bool(move, "sprint");
                JsonObject look = object(root, "look");
                keys(look, "yaw_delta_degrees", "pitch_delta_degrees");
                float yaw = angle(look, "yaw_delta_degrees"), pitch = angle(look, "pitch_delta_degrees");
                JsonObject operation = root.get("operation").isJsonNull() ? null : object(root, "operation");
                if (operation != null) validateOperation(operation);
                Long cancel = root.get("cancel_request_sequence_id").isJsonNull() ? null
                        : integer(root, "cancel_request_sequence_id", 0, Long.MAX_VALUE);
                FirstEffectWindow window = null;
                SafeDefault safeDefault = null;
                if (schema.equals("mc2p.client_action.v2")) {
                    if (!root.get("first_effect_window").isJsonNull()) {
                        JsonObject windowValue = object(root, "first_effect_window");
                        keys(windowValue, "earliest_tick", "latest_tick");
                        long earliest = integer(windowValue, "earliest_tick", 0, Long.MAX_VALUE);
                        long latest = integer(windowValue, "latest_tick", 0, Long.MAX_VALUE);
                        if (latest < earliest) throw new IllegalArgumentException("reversed first effect window");
                        window = new FirstEffectWindow(earliest, latest);
                    }
                    if (!root.get("safe_default").isJsonNull()) {
                        JsonObject defaultValue = object(root, "safe_default");
                        keys(defaultValue, "activation_tick", "movement", "valid_for_ticks", "wall_budget_ns");
                        long activation = integer(defaultValue, "activation_tick", 0, Long.MAX_VALUE);
                        JsonObject defaultMove = object(defaultValue, "movement");
                        keys(defaultMove, "forward", "strafe", "jump", "sneak", "sprint");
                        Movement movement = new Movement(
                                (int) integer(defaultMove, "forward", -1, 1),
                                (int) integer(defaultMove, "strafe", -1, 1),
                                bool(defaultMove, "jump"), bool(defaultMove, "sneak"),
                                bool(defaultMove, "sprint"));
                        int duration = (int) integer(defaultValue, "valid_for_ticks", 1, 20);
                        long wallBudget = integer(defaultValue, "wall_budget_ns", 1, 30_000_000_000L);
                        if (wallBudget < duration * 50_000_000L)
                            throw new IllegalArgumentException("safe default wall budget");
                        safeDefault = new SafeDefault(activation, movement, duration, wallBudget);
                    }
                }
                if (cancel != null && (cancel >= sequence || operation != null || forward != 0
                        || strafe != 0 || jump || sneak || sprint || yaw != 0 || pitch != 0
                        || window != null || safeDefault != null))
                    throw new IllegalArgumentException("cancel cannot introduce new behavior");
                return new ClientActionRequest(episode, sequence, observation, budget, observationBudget, ticks,
                        forward, strafe, jump, sneak, sprint, yaw, pitch, operation, cancel, window, safeDefault);
            }
        } catch (IOException | IllegalStateException | NumberFormatException error) {
            throw new IllegalArgumentException("invalid action payload", error);
        }
    }

    private static void validateOperation(JsonObject op) {
        switch (string(op, "kind")) {
            case "open_inventory", "close_screen" -> keys(op, "kind");
            case "select_hotbar" -> {
                keys(op, "kind", "slot"); integer(op, "slot", 0, 8);
            }
            case "interact_block", "mine_block" -> {
                keys(op, "kind", "block_x", "block_y", "block_z", "face");
                for (String coordinate : new String[]{"block_x", "block_y", "block_z"})
                    integer(op, coordinate, -30_000_000, 30_000_000);
                if (!Set.of("down", "up", "north", "south", "west", "east").contains(string(op, "face")))
                    throw new IllegalArgumentException("block face");
            }
            case "attack_entity" -> {
                keys(op, "kind", "entity_ref", "minimum_cooldown_progress");
                if (!string(op, "entity_ref").matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"))
                    throw new IllegalArgumentException("entity reference");
                number(op, "minimum_cooldown_progress", 0.0, 1.0);
            }
            case "click_slot" -> {
                keys(op, "kind", "gui_session_id", "sync_id", "expected_revision", "slot", "button", "click_type");
                if (!string(op, "gui_session_id").matches("[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"))
                    throw new IllegalArgumentException("GUI session");
                integer(op, "sync_id", 0, Integer.MAX_VALUE);
                integer(op, "expected_revision", 0, Integer.MAX_VALUE);
                integer(op, "slot", 0, 1023);
                String click = string(op, "click_type");
                if (!Set.of("pickup", "quick_move", "swap", "throw", "pickup_all").contains(click))
                    throw new IllegalArgumentException("unsupported click");
                integer(op, "button", 0, click.equals("swap") ? 8 : 1);
            }
            default -> throw new IllegalArgumentException("unsupported operation");
        }
    }

    private static JsonElement read(JsonReader reader, int depth) throws IOException {
        if (depth > 4) throw new IllegalArgumentException("action nesting");
        switch (reader.peek()) {
            case BEGIN_OBJECT:
                JsonObject result = new JsonObject();
                reader.beginObject();
                while (reader.hasNext()) {
                    String name = reader.nextName();
                    if (result.has(name)) throw new IllegalArgumentException("duplicate key");
                    result.add(name, read(reader, depth + 1));
                }
                reader.endObject();
                return result;
            case STRING: return new JsonPrimitive(reader.nextString());
            case NUMBER: return new JsonPrimitive(new java.math.BigDecimal(reader.nextString()));
            case BOOLEAN: return new JsonPrimitive(reader.nextBoolean());
            case NULL: reader.nextNull(); return JsonNull.INSTANCE;
            default: throw new IllegalArgumentException("unsupported JSON value");
        }
    }

    private static void keys(JsonObject object, String... keys) {
        if (!object.keySet().equals(Set.of(keys))) throw new IllegalArgumentException("unexpected keys");
    }
    private static JsonObject object(JsonObject root, String name) {
        JsonElement value = root.get(name);
        if (value == null || !value.isJsonObject()) throw new IllegalArgumentException(name);
        return value.getAsJsonObject();
    }
    private static JsonPrimitive primitive(JsonObject root, String name) {
        JsonElement value = root.get(name);
        if (value == null || !value.isJsonPrimitive()) throw new IllegalArgumentException(name);
        return value.getAsJsonPrimitive();
    }
    private static String string(JsonObject root, String name) {
        JsonPrimitive value = primitive(root, name);
        if (!value.isString()) throw new IllegalArgumentException(name);
        return value.getAsString();
    }
    private static boolean bool(JsonObject root, String name) {
        JsonPrimitive value = primitive(root, name);
        if (!value.isBoolean()) throw new IllegalArgumentException(name);
        return value.getAsBoolean();
    }
    private static long integer(JsonObject root, String name, long min, long max) {
        JsonPrimitive value = primitive(root, name);
        if (!value.isNumber() || !value.getAsString().matches("-?[0-9]+")) throw new IllegalArgumentException(name);
        long number = Long.parseLong(value.getAsString());
        if (number < min || number > max) throw new IllegalArgumentException(name);
        return number;
    }
    private static float angle(JsonObject root, String name) {
        return (float) number(root, name, -180, 180);
    }
    private static double number(JsonObject root, String name, double min, double max) {
        JsonPrimitive value = primitive(root, name);
        if (!value.isNumber()) throw new IllegalArgumentException(name);
        double number = value.getAsDouble();
        if (!Double.isFinite(number) || number < min || number > max) throw new IllegalArgumentException(name);
        return number;
    }
}
