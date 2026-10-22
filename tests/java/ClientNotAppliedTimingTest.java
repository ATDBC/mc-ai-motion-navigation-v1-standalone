import com.mc2p.actions.ClientTimingSchedule;

public final class ClientNotAppliedTimingTest {
    public static void main(String[] args) {
        ClientTimingSchedule schedule = new ClientTimingSchedule(() -> 0L, () -> true);
        schedule.notApplied(27L, "ep", 26L);
        var events = schedule.drainEvents();
        if (events.size() != 1) throw new AssertionError("missing not-applied event");
        var event = events.get(0);
        if (!event.status().equals("not_applied")
                || event.movementTickId() != 27L
                || !event.episodeId().equals("ep")
                || event.requestSequenceId() != 26L) {
            throw new AssertionError("not-applied identity mismatch");
        }
        System.out.println("CLIENT_NOT_APPLIED_TIMING_OK");
    }
}
