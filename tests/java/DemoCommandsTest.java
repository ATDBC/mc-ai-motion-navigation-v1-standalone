import com.mc2p.democontrol.DemoCommands;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import net.fabricmc.fabric.api.client.command.v2.FabricClientCommandSource;
import java.util.ArrayList;

public class DemoCommandsTest {
    public static void main(String[] args) throws Exception {
        var dispatcher = new CommandDispatcher<FabricClientCommandSource>();
        var submitted = new ArrayList<DemoCommands.DemoCommand>();
        DemoCommands.register(dispatcher, submitted::add);
        String[] commands = {"bot follow start", "bot follow stop", "bot follow status", "bot demo stop"};
        String[] kinds = {"follow_start", "follow_stop", "follow_status", "demo_stop"};
        for (int i = 0; i < commands.length; i++) {
            dispatcher.execute(commands[i], null);
            if (!submitted.getLast().kind().equals(kinds[i])) throw new AssertionError("wrong command");
        }
        for (String mode : new String[]{"slow", "normal", "fast", "max", "auto"}) {
            dispatcher.execute("bot follow mode " + mode, null);
            if (!submitted.getLast().kind().equals("follow_mode") || !submitted.getLast().mode().equals(mode))
                throw new AssertionError("wrong fixed/auto mode");
        }
        for (double distance : new double[]{1.5, 3, 6}) {
            dispatcher.execute("bot follow distance " + distance, null);
            if (submitted.getLast().distanceBlocks() != distance) throw new AssertionError("wrong distance");
        }
        int count = submitted.size();
        for (String bad : new String[]{"bot tp", "bot follow mode turbo", "bot follow distance 1.49",
                "bot follow distance 6.01", "bot follow distance NaN", "bot follow start extra", "bot follow"}) {
            try { dispatcher.execute(bad, null); throw new AssertionError("bad command accepted: " + bad); }
            catch (CommandSyntaxException expected) {}
        }
        if (count != submitted.size()) throw new AssertionError("invalid command emitted intent");
        System.out.println("DEMO_COMMANDS_OK");
    }
}
