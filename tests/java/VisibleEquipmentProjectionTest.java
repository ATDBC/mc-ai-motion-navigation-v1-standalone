import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mc2p.observation.ClientObservationCollector;
import java.lang.reflect.Method;
import java.util.Set;
import net.minecraft.Bootstrap;
import net.minecraft.SharedConstants;
import net.minecraft.component.DataComponentTypes;
import net.minecraft.client.render.entity.feature.FeatureRendererContext;
import net.minecraft.client.render.entity.feature.HeldItemFeatureRenderer;
import net.minecraft.client.render.entity.model.ArmorStandEntityModel;
import net.minecraft.client.render.model.json.ModelTransformationMode;
import net.minecraft.client.render.VertexConsumerProvider;
import net.minecraft.client.util.math.MatrixStack;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.entity.passive.CowEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.item.Items;
import net.minecraft.text.Text;
import net.minecraft.util.Arm;
import net.minecraft.util.Identifier;

/** Test-only detached vanilla objects; no server, world edits, actors or renderer. */
public class VisibleEquipmentProjectionTest {
    public static void main(String[] args) throws Exception {
        SharedConstants.createGameVersion();
        Bootstrap.initialize();
        ArmorStandEntity stand = new ArmorStandEntity(EntityType.ARMOR_STAND, null);
        // This detached fixture tests field projection only, not renderer/slot visibility.
        Method collect = ClientObservationCollector.class.getDeclaredMethod("visibleEquipment", Entity.class);
        collect.setAccessible(true);
        ItemStack first = new ItemStack(Items.DIAMOND_SWORD);
        first.setDamage(137);
        first.set(DataComponentTypes.CUSTOM_NAME, Text.literal("secret sword"));
        stand.equipStack(EquipmentSlot.MAINHAND, first);
        verifyNativeHiddenArmsStillDispatchItem(stand);
        JsonObject sword = ((JsonArray) collect.invoke(null, stand)).get(0).getAsJsonObject().getAsJsonObject("item");
        requireItem(sword, "minecraft:diamond_sword");
        first.setDamage(953);
        first.set(DataComponentTypes.CUSTOM_NAME, Text.literal("another secret"));
        JsonObject changed = ((JsonArray) collect.invoke(null, stand)).get(0).getAsJsonObject().getAsJsonObject("item");
        if (!sword.equals(changed)) throw new AssertionError("hidden damage/name affects projection");
        ItemStack sticks = new ItemStack(Items.STICK, 1);
        stand.equipStack(EquipmentSlot.MAINHAND, sticks);
        JsonObject one = ((JsonArray) collect.invoke(null, stand)).get(0).getAsJsonObject().getAsJsonObject("item");
        requireItem(one, "minecraft:stick");
        sticks.setCount(64);
        JsonObject many = ((JsonArray) collect.invoke(null, stand)).get(0).getAsJsonObject().getAsJsonObject("item");
        if (!one.equals(many)) throw new AssertionError("hidden count affects projection");
        stand.equipStack(EquipmentSlot.MAINHAND, ItemStack.EMPTY);
        JsonObject empty = ((JsonArray) collect.invoke(null, stand)).get(0).getAsJsonObject().getAsJsonObject("item");
        if (!empty.keySet().equals(Set.of("empty", "item_id")) || !empty.get("empty").getAsBoolean()
                || !empty.get("item_id").isJsonNull()) throw new AssertionError(empty);
        // Native CowEntityRenderer/MobEntityRenderer/LivingEntityRenderer register no equipment feature.
        CowEntity cow = new CowEntity(EntityType.COW, new DetachedTestWorld());
        cow.equipStack(EquipmentSlot.MAINHAND, new ItemStack(Items.IRON_AXE));
        if (!cow.getMainHandStack().isOf(Items.IRON_AXE)) throw new AssertionError("negative control has no cached item");
        JsonArray cowEquipment = (JsonArray) collect.invoke(null, cow);
        if (!cowEquipment.isEmpty()) throw new AssertionError("cow cached hidden equipment leaked: " + cowEquipment);
        System.out.println("VISIBLE_EQUIPMENT_PROJECTION_OK");
    }

    private static void verifyNativeHiddenArmsStillDispatchItem(ArmorStandEntity stand) {
        // Invoke native feature selection and arm transform, but never any item drawing/buffer.
        // ShowArms hides wood, not the item: do not add a false shouldShowArms filter.
        ArmorStandEntityModel model = new ArmorStandEntityModel(ArmorStandEntityModel.getTexturedModelData().createModel());
        model.setAngles(stand, 0, 0, 0, 0, 0);
        if (stand.shouldShowArms() || model.rightArm.visible) throw new AssertionError("arms not hidden");
        FeatureRendererContext<ArmorStandEntity, ArmorStandEntityModel> context = new FeatureRendererContext<>() {
            public ArmorStandEntityModel getModel() { return model; }
            public Identifier getTexture(ArmorStandEntity entity) { return Identifier.ofVanilla("test"); }
        };
        int[] dispatches = {0};
        var feature = new HeldItemFeatureRenderer<ArmorStandEntity, ArmorStandEntityModel>(context, null) {
            @Override protected void renderItem(LivingEntity entity, ItemStack stack, ModelTransformationMode mode,
                    Arm arm, MatrixStack matrices, VertexConsumerProvider vertices, int light) {
                if (!stack.isEmpty()) {
                    model.setArmAngle(arm, matrices);
                    dispatches[0]++;
                }
            }
        };
        feature.render(new MatrixStack(), layer -> { throw new AssertionError("test attempted actual drawing"); },
                0, stand, 0, 0, 0, 0, 0, 0);
        if (dispatches[0] != 1 || model.rightArm.visible) throw new AssertionError("native hidden-arm item dispatch changed");
    }

    private static void requireItem(JsonObject item, String id) {
        if (!item.keySet().equals(Set.of("empty", "item_id")) || item.get("empty").getAsBoolean()
                || !item.get("item_id").getAsString().equals(id)) throw new AssertionError(item);
    }
}
