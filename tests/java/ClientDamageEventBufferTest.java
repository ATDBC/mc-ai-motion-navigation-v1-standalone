import com.mc2p.observation.ClientDamageEventBuffer;

public final class ClientDamageEventBufferTest {
    private static void require(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }

    public static void main(String[] args) {
        ClientDamageEventBuffer buffer = new ClientDamageEventBuffer();
        Object world = new Object();
        buffer.record(world, 10, 7, "minecraft:player_attack", 1, 1);
        var first = buffer.snapshot(world, 3, 1, id -> id == 7 ? "entity-7" : null);
        require(first.events().size() == 1, "recorded damage missing");
        var event = first.events().getFirst();
        require(event.targetEntityRef().equals("entity-7"), "target identity missing");
        require(event.sourceIsSelf() && event.directSourceIsSelf(), "self source lost");
        require(first.droppedCount() == 0, "unexpected drop");

        var repeated = buffer.snapshot(world, 3, 1, id -> id == 7 ? "entity-7" : null);
        require(repeated.events().size() == 1, "same generation acknowledged too early");
        var acknowledged = buffer.snapshot(world, 4, 1, id -> "entity-7");
        require(acknowledged.events().isEmpty(), "next generation did not acknowledge delivery");

        buffer.record(world, 11, 1, "minecraft:fall", -1, -1);
        var self = buffer.snapshot(world, 5, 1, id -> null).events().getFirst();
        require(self.targetIsSelf() && self.targetEntityRef() == null, "self target lost");
        require(!self.sourceEntityPresent() && !self.directEntityPresent(), "environment invented entity");

        Object otherWorld = new Object();
        require(buffer.snapshot(otherWorld, 6, 1, id -> null).events().isEmpty(),
                "world replacement retained damage");

        for (int i = 0; i < 65; i++) {
            buffer.record(otherWorld, 20 + i, 1, "minecraft:generic", -1, -1);
        }
        var bounded = buffer.snapshot(otherWorld, 7, 1, id -> null);
        require(bounded.events().size() == 64, "damage buffer is not bounded");
        require(bounded.droppedCount() == 1, "oldest drop was not reported");
        require(bounded.events().getFirst().worldTick() == 21, "wrong event was dropped");

        System.out.println("CLIENT_DAMAGE_EVENT_BUFFER_OK");
    }
}
