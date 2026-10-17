package com.mc2p.observation;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.util.function.IntUnaryOperator;

/** Read only explicitly mapped, public current-handler properties; unknown is not empty success. */
public final class ClientGuiProperties {
    private ClientGuiProperties() {}
    public static JsonObject collect(String handlerType, int count, IntUnaryOperator read) {
        int expected = switch (handlerType) {
            case "minecraft:furnace", "minecraft:blast_furnace", "minecraft:smoker" -> 4;
            case "closed", "minecraft:player", "minecraft:crafting", "minecraft:hopper", "minecraft:shulker_box",
                 "minecraft:generic_3x3", "minecraft:generic_9x1", "minecraft:generic_9x2", "minecraft:generic_9x3",
                 "minecraft:generic_9x4", "minecraft:generic_9x5", "minecraft:generic_9x6" -> 0;
            default -> -1;
        };
        String reason = expected < 0 ? "unmapped_public_properties" : count != expected ? "property_layout_mismatch" : null;
        JsonObject result = new JsonObject();
        JsonArray entries = new JsonArray();
        if (reason == null) {
            for (int id = 0; id < expected; id++) {
                int value = read.applyAsInt(id);
                if (value < 0 || value > 32767) throw new IllegalStateException("invalid vanilla furnace property");
                JsonObject entry = new JsonObject();
                entry.addProperty("property_id", id);
                entry.addProperty("value", value);
                entries.add(entry);
            }
        }
        result.add("properties", entries);
        result.addProperty("properties_status", reason == null ? "valid" : "unsupported");
        if (reason == null) result.add("properties_reason_code", JsonNull.INSTANCE);
        else result.addProperty("properties_reason_code", reason);
        return result;
    }
}
