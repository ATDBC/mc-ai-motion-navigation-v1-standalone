import com.google.gson.JsonObject;
import com.mc2p.observation.ClientEntityIndex;
import com.mc2p.observation.ClientObservationCollector;
import com.mc2p.observation.ClientObservationRequestV3;
import java.lang.reflect.Method;
import java.util.List;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.mob.ZombieEntity;
import net.minecraft.entity.ItemEntity;
import net.minecraft.util.math.Vec3d;

public final class TrackedEntityObservationTest {
    private static void require(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }
    private static JsonObject group(long tick, ClientObservationRequestV3 request,
            ClientEntityIndex index, Vec3d position, Vec3d velocity, float yaw) throws Exception {
        Method method = ClientObservationCollector.class.getDeclaredMethod("trackedEntityGroup",
                long.class, ClientObservationRequestV3.class, ClientEntityIndex.class,
                Vec3d.class, Vec3d.class, float.class);
        method.setAccessible(true);
        return (JsonObject) method.invoke(null, tick, request, index, position, velocity, yaw);
    }
    public static void main(String[] args) throws Exception {
        SharedConstants.createGameVersion();
        Bootstrap.initialize();
        var world = new DetachedTestWorld();
        var zombie = new ZombieEntity(EntityType.ZOMBIE, world);
        zombie.setPosition(4.0, 65.0, -2.0);
        zombie.setVelocity(.1, 0, .2);
        zombie.setYaw(35f);
        zombie.setPitch(-10f);
        zombie.setHealth(7f);
        var index = new ClientEntityIndex();
        index.beginFrame(world, 1, List.of(zombie));
        index.offer(zombie, zombie.getPos(), "minecraft:zombie");
        String trackId = index.trackId(zombie);
        var requested = new ClientObservationRequestV3("navigation_v1", List.of(), trackId);
        JsonObject valid = group(41, requested, index, new Vec3d(1, 64, -1),
                new Vec3d(.02, 0, .03), 5f);
        JsonObject value = valid.getAsJsonObject("value");
        require(valid.get("status").getAsString().equals("valid"), "living entity missing");
        require(value.get("track_id").getAsString().equals(trackId), "identity changed");
        require(value.get("entity_type").getAsString().equals("minecraft:zombie"), "type changed");
        require(value.getAsJsonObject("relative_position").get("x").getAsDouble() == 3.0, "relative position wrong");
        require(Math.abs(value.getAsJsonObject("relative_velocity").get("x").getAsDouble() - .08) < 1e-9,
                "relative velocity wrong");
        require(value.get("relative_yaw_degrees").getAsDouble() == 30.0, "relative yaw wrong");
        require(value.get("pitch_degrees").getAsDouble() == -10.0, "pitch wrong");
        require(!value.get("is_on_ground").getAsBoolean(), "ground state lost");
        require(value.get("is_loaded").getAsBoolean(), "loaded state lost");
        require(!value.get("is_dead").getAsBoolean(), "living zombie marked dead");
        require(value.get("health_points").getAsDouble() == 7.0, "health lost");
        require(value.get("max_health_points").getAsDouble() == 20.0, "max health lost");
        require(value.getAsJsonObject("bounding_box_size").get("y").getAsDouble() > 1.0,
                "bounding box lost");

        JsonObject none = group(42, new ClientObservationRequestV3("navigation_v1"), index,
                Vec3d.ZERO, Vec3d.ZERO, 0f);
        require(none.get("reason_code").getAsString().equals("not_requested"), "absent query not explicit");
        JsonObject unknown = group(42, new ClientObservationRequestV3("navigation_v1", List.of(), "entity-missing"),
                index, Vec3d.ZERO, Vec3d.ZERO, 0f);
        require(unknown.get("reason_code").getAsString().equals("entity_unavailable"), "unknown entity invented");

        var item = new ItemEntity(EntityType.ITEM, world);
        index.beginFrame(world, 2, List.of(item));
        index.offer(item, Vec3d.ZERO, "minecraft:item");
        JsonObject nonLiving = group(43, new ClientObservationRequestV3("navigation_v1", List.of(), index.trackId(item)),
                index, Vec3d.ZERO, Vec3d.ZERO, 0f);
        require(nonLiving.get("reason_code").getAsString().equals("not_living_entity"), "nonliving entity exposed");

        zombie.setHealth(0f);
        index.beginFrame(world, 3, List.of(zombie));
        index.offer(zombie, zombie.getPos(), "minecraft:zombie");
        JsonObject dead = group(44, new ClientObservationRequestV3("navigation_v1", List.of(), index.trackId(zombie)),
                index, Vec3d.ZERO, Vec3d.ZERO, 0f).getAsJsonObject("value");
        require(dead.get("is_dead").getAsBoolean() && dead.get("health_points").getAsDouble() == 0.0,
                "dead/health facts were merged or lost");
        System.out.println("TRACKED_ENTITY_OBSERVATION_OK");
    }
}
