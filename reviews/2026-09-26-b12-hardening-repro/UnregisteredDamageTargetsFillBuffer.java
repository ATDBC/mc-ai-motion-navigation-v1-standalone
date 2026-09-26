import com.mc2p.observation.ClientDamageEventBuffer;

/**
 * Damage events for entities without a current track id are never delivered
 * and never acknowledged, so they stay in the bounded buffer.
 *
 * Build and run from the repository root of a checkout of commit 211af42:
 *
 *   mkdir -p /tmp/b12buf
 *   javac -d /tmp/b12buf \
 *     mc2p/backends/runtime_overlays/mc121_observation/ClientDamageEventBuffer.java \
 *     reviews/2026-09-26-b12-hardening-repro/UnregisteredDamageTargetsFillBuffer.java
 *   java -cp /tmp/b12buf UnregisteredDamageTargetsFillBuffer
 *
 * ClientDamageEventBuffer.snapshot skips an event whose target has no track id
 * (``continue``) without setting its delivered generation, and the next
 * snapshot only removes delivered events.  Mobs that take damage outside the
 * formal perception set (for example undead burning in daylight behind the
 * player) therefore accumulate until the 64-slot buffer overflows, and every
 * later event is counted in ``damage_events_dropped``.  Attribution stays
 * correct because stale events are filtered by world tick; the dropped count
 * stops meaning "relevant evidence was lost".
 *
 * Observed at 211af42: one burn event for an unregistered zombie plus one robot
 * hit on the tracked target per snapshot; the dropped count is 0 after 11
 * rounds, 2 after 64 and 8 after 70, while all 70 robot hits were delivered.
 */
public final class UnregisteredDamageTargetsFillBuffer {
    public static void main(String[] args) {
        ClientDamageEventBuffer buffer = new ClientDamageEventBuffer();
        Object world = new Object();
        int self = 1;
        int trackedTarget = 7;
        int unseenZombie = 99;
        long tick = 100;
        long generation = 1;
        int deliveredRobotHits = 0;
        for (int second = 0; second < 70; second++) {
            buffer.record(world, tick++, unseenZombie, "minecraft:on_fire", -1, -1);
            buffer.record(world, tick++, trackedTarget, "minecraft:player_attack", self, self);
            var snapshot = buffer.snapshot(world, generation++, self,
                    id -> id == trackedTarget ? "entity-7" : null);
            deliveredRobotHits += (int) snapshot.events().stream()
                    .filter(event -> "entity-7".equals(event.targetEntityRef()))
                    .count();
            if (second == 10 || second == 63 || second == 69) {
                System.out.println("after " + (second + 1) + " rounds: delivered events="
                        + snapshot.events().size() + " dropped=" + snapshot.droppedCount());
            }
        }
        System.out.println("robot hits delivered (each counted once per generation it appears): "
                + deliveredRobotHits);
    }
}
