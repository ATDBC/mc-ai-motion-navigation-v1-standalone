import com.google.gson.JsonObject;
import com.mc2p.observation.ClientObservationCollector;
import java.lang.reflect.Method;
import java.util.Set;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.shape.VoxelShape;
import net.minecraft.util.shape.VoxelShapes;

/** Invoke the actual shared sampler/filter geometry without graphics or world truth. */
public class ClientPerceptionProfileTest {
    public static void main(String[] args) throws Exception {
        Method metadata = method("perceptionMetadata");
        JsonObject meta = (JsonObject) metadata.invoke(null);
        require(meta.get("sensor_profile_revision").getAsInt() == 3, "wrong emitted revision");
        require(meta.get("horizontal_fov_degrees").getAsDouble() == 120., "wrong horizontal metadata");
        require(meta.get("vertical_fov_degrees").getAsDouble() == 120., "wrong vertical metadata");
        require(meta.get("ray_columns").getAsInt() == 159, "wrong dense ray columns");
        require(meta.get("ray_rows").getAsInt() == 9, "wrong dense ray rows");
        require(meta.get("ray_columns").getAsInt() * meta.get("ray_rows").getAsInt() == 1431,
                "wrong dense sample count");
        Method direction = method("rayDirection", float.class, float.class, int.class, int.class);
        Vec3d center = (Vec3d) direction.invoke(null, 0f, 0f, 4, 79);
        near(center.x, 0.); near(center.y, 0.); near(center.z, 1.);
        Method rayId = method("rayId", int.class, int.class);
        require((int) rayId.invoke(null, 0, 0) == 0, "wrong first ray id");
        require((int) rayId.invoke(null, 4, 79) == 715, "wrong center ray id");
        require((int) rayId.invoke(null, 8, 158) == 1430, "wrong final ray id");
        Vec3d firstRight = (Vec3d) direction.invoke(null, 0f, 0f, 4, 80);
        double firstOffset = Math.toRadians(.7594936708860759);
        near(firstRight.x, -Math.sin(firstOffset)); near(firstRight.y, 0.);
        near(firstRight.z, Math.cos(firstOffset));
        Vec3d lower = (Vec3d) direction.invoke(null, 0f, 0f, 8, 79);
        near(lower.x, 0.); near(lower.y, -Math.sqrt(3.) / 2.); near(lower.z, .5);
        Vec3d right = (Vec3d) direction.invoke(null, 0f, 0f, 4, 158);
        near(right.x, -Math.sqrt(3.) / 2.); near(right.y, 0.); near(right.z, .5);
        Vec3d upper = (Vec3d) direction.invoke(null, 0f, 0f, 0, 79);
        near(upper.y, Math.sqrt(3.) / 2.);
        Vec3d turned = (Vec3d) direction.invoke(null, 90f, 15f, 4, 79);
        near(turned.x, -Math.cos(Math.PI / 12.)); near(turned.y, -Math.sin(Math.PI / 12.));
        // Preserve the declared additive angular grid near poles; no silent clamp.
        Vec3d overPole = (Vec3d) direction.invoke(null, 0f, 75f, 8, 79);
        near(overPole.y, -Math.sqrt(.5)); near(overPole.z, -Math.sqrt(.5));
        Vec3d abovePole = (Vec3d) direction.invoke(null, 0f, -75f, 0, 79);
        near(abovePole.y, Math.sqrt(.5)); near(abovePole.z, -Math.sqrt(.5));
        Method within = method("withinEntityFov", double.class, double.class);
        for (double[] pair : new double[][]{{0, 0}, {55, 55}, {-60, -60}, {60, 60}})
            require((boolean) within.invoke(null, pair[0], pair[1]), "valid wide center rejected");
        for (double[] pair : new double[][]{{60.01, 0}, {0, -60.01}, {180, 0}})
            require(!(boolean) within.invoke(null, pair[0], pair[1]), "outside center accepted");
        verifyBodyContactGeometry();
        System.out.println("CLIENT_PERCEPTION_PROFILE_OK");
    }

    private static void verifyBodyContactGeometry() throws Exception {
        Method intersects = method("intersectsCollisionShape", Box.class, BlockPos.class, VoxelShape.class);
        // A standing 0.6-wide body centered on both block boundaries overlaps all
        // four support cubes after the collector's unchanged 0.05 contact expansion.
        Box body = new Box(.7, 64., .7, 1.3, 65.8, 1.3).expand(.05);
        Set<BlockPos> supports = Set.of(
                new BlockPos(0, 63, 0), new BlockPos(1, 63, 0),
                new BlockPos(0, 63, 1), new BlockPos(1, 63, 1));
        for (BlockPos position : supports) {
            require((boolean) intersects.invoke(null, body, position, VoxelShapes.fullCube()),
                    "straddled underfoot support omitted: " + position);
        }
        require(!(boolean) intersects.invoke(null, body, new BlockPos(-1, 63, 0), VoxelShapes.fullCube()),
                "non-contact horizontal neighbor included");
        require(!(boolean) intersects.invoke(null, body, new BlockPos(0, 62, 0), VoxelShapes.fullCube()),
                "non-contact block below support included");
        VoxelShape lowSlab = VoxelShapes.cuboid(0., 0., 0., 1., .5, 1.);
        require(!(boolean) intersects.invoke(null, body, new BlockPos(0, 63, 0), lowSlab),
                "non-contact partial collision shape included");
    }
    private static Method method(String name, Class<?>... types) throws Exception {
        Method result = ClientObservationCollector.class.getDeclaredMethod(name, types);
        result.setAccessible(true);
        return result;
    }
    private static void near(double actual, double expected) {
        // Minecraft's float sine lookup is approximate, not double trigonometry.
        require(Math.abs(actual - expected) < .0002, "incorrect sampled direction " + actual + " != " + expected);
    }
    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
