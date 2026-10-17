import com.mc2p.observation.ClientGuiProperties;
import net.minecraft.screen.Property;
import java.util.List;

public final class ClientGuiPropertiesTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        int[] values = {1400, 1600, 80, 200};
        var properties = List.of(Property.create(values, 0), Property.create(values, 1),
                                 Property.create(values, 2), Property.create(values, 3));
        for (String type : new String[] {"minecraft:furnace", "minecraft:blast_furnace", "minecraft:smoker"}) {
            var output = ClientGuiProperties.collect(type, properties.size(), i -> properties.get(i).get());
            yes(output.get("properties_status").getAsString().equals("valid"));
            yes(output.getAsJsonArray("properties").size() == 4);
            yes(output.getAsJsonArray("properties").get(2).getAsJsonObject().get("value").getAsInt() == 80);
        }
        properties.get(2).set(81);
        yes(ClientGuiProperties.collect("minecraft:furnace", 4, i -> properties.get(i).get())
            .getAsJsonArray("properties").get(2).getAsJsonObject().get("value").getAsInt() == 81);
        var unknown = ClientGuiProperties.collect("mod:private_handler", 100, i -> { throw new AssertionError("private read"); });
        yes(unknown.get("properties_status").getAsString().equals("unsupported"));
        yes(unknown.getAsJsonArray("properties").isEmpty());
        var wrongLayout = ClientGuiProperties.collect("minecraft:furnace", 5, i -> { throw new AssertionError("layout read"); });
        yes(wrongLayout.get("properties_reason_code").getAsString().equals("property_layout_mismatch"));
        for (String type : new String[] {"minecraft:player", "minecraft:generic_9x3", "closed"}) {
            var empty = ClientGuiProperties.collect(type, 0, i -> { throw new AssertionError("empty read"); });
            yes(empty.get("properties_status").getAsString().equals("valid"));
            yes(empty.getAsJsonArray("properties").isEmpty());
        }
        System.out.println("CLIENT_GUI_PROPERTIES_OK");
    }
}
