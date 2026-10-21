import com.mc2p.actions.ClientTimingSchedule;

public final class ClientTimingScheduleTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    private static ClientTimingSchedule.Movement move(boolean jump, boolean sneak) {
        return new ClientTimingSchedule.Movement(1, 0, jump, sneak, true);
    }

    public static void main(String[] args) {
        long[] now = {1_000_000_000L};
        boolean[] allowed = {true};
        ClientTimingSchedule schedule = new ClientTimingSchedule(() -> now[0], () -> allowed[0]);
        schedule.armDefault("ep", 25, 27, move(false, true), 8, 400_000_000L);
        schedule.parentSampled(26, "ep", 25);
        yes(schedule.drainEvents().getFirst().status().equals("fallback_armed"));

        schedule.admitPending("ep", 26, 9, 27, 27, now[0] + 1_000_000_000L, 1, move(true, false), "payload-26", null);
        yes(!schedule.responseReady());
        var pendingEvent = schedule.drainEvents().getFirst();
        yes(pendingEvent.status().equals("window_pending") && pendingEvent.movementTickId() == 9);
        yes(schedule.prepare(27, false).kind() == ClientTimingSchedule.Kind.PENDING);
        schedule.confirmPendingActivation();
        schedule.sampled(27);
        yes(schedule.drainEvents().getFirst().status().equals("window_applied"));
        yes(schedule.responseReady());

        schedule.reset();
        schedule.armDefault("ep", 30, 108, move(false, true), 11, 550_000_000L);
        schedule.parentSampled(107, "ep", 30);
        schedule.admitPending("ep", 31, 107, 108, 108, now[0] + 1_000_000_000L, 1, move(true, false), "payload-31", null);
        yes(schedule.prepare(109, false).kind() == ClientTimingSchedule.Kind.DEFAULT);
        var missed = schedule.drainEvents();
        yes(missed.stream().anyMatch(e -> e.status().equals("window_missed")));
        yes(missed.stream().anyMatch(e -> e.status().equals("default_active")));
        yes(!schedule.responseReady());
        for (long tick = 109; tick < 120; tick++) {
            if (tick != 109) yes(schedule.prepare(tick, false).kind() == ClientTimingSchedule.Kind.DEFAULT);
            schedule.sampled(tick);
            now[0] += 100_000_000L; // Wall expiry must not truncate proved duration.
        }
        yes(schedule.drainEvents().stream().noneMatch(e -> e.status().equals("default_exhausted")));
        yes(!schedule.responseReady());
        yes(schedule.prepare(120, false).kind() == ClientTimingSchedule.Kind.CURRENT);
        var exhausted = schedule.drainEvents().getFirst();
        yes(exhausted.status().equals("default_exhausted") && exhausted.movementTickId() == 120);
        yes(schedule.responseReady());

        schedule.reset();
        schedule.armDefault("ep", 40, 201, move(false, true), 8, 400_000_000L);
        schedule.admitPending("ep", 41, 200, 202, 202, now[0] + 1_000_000_000L, 1, move(true, false), "old", null);
        schedule.admitPending("ep", 42, 200, 203, 203, now[0] + 1_000_000_000L, 1, move(false, false), "new", null);
        yes(schedule.pendingSequence() == 42);
        schedule.cancelAll();
        yes(schedule.prepare(203, false).kind() == ClientTimingSchedule.Kind.CURRENT);

        schedule.armDefault("ep", 50, 301, move(false, true), 8, 400_000_000L);
        allowed[0] = false;
        yes(schedule.prepare(301, false).kind() == ClientTimingSchedule.Kind.NEUTRAL);
        allowed[0] = true;

        schedule.reset();
        schedule.armDefault("ep", 95, 731, move(false, true), 8, 400_000_000L);
        schedule.admitPending("ep", 96, 730, 731, 731, now[0] + 1_000_000_000L,
                1, move(true, false), "installed-pending", null);
        yes(schedule.rejectInstalled(96));
        yes(schedule.prepare(731, false).kind() == ClientTimingSchedule.Kind.DEFAULT);
        yes(schedule.holdRejectedResponseForFallback());
        yes(!schedule.responseReady());
        yes(schedule.rejectInstalled(95));
        yes(schedule.prepare(731, false).kind() == ClientTimingSchedule.Kind.CURRENT);
        yes(schedule.prepare(301, false).kind() == ClientTimingSchedule.Kind.CURRENT);

        schedule.reset();
        schedule.armDefault("ep", 55, 350, move(false, true), 8, 400_000_000L);
        schedule.admitPending("ep", 56, 349, 350, 350, now[0] + 1_000_000_000L,
                1, move(true, false), "rejected", null);
        yes(schedule.prepare(350, false).kind() == ClientTimingSchedule.Kind.PENDING);
        schedule.rejectPreparedPending(350);
        yes(schedule.prepare(350, false).kind() == ClientTimingSchedule.Kind.DEFAULT);
        yes(!schedule.responseReady());

        schedule.reset();
        schedule.admitPending("ep", 60, 400, 399, 399, now[0] + 1_000_000_000L,
                1, move(true, false), "no-fallback", null);
        yes(schedule.prepare(400, false).kind() == ClientTimingSchedule.Kind.CURRENT);
        yes(schedule.responseReady()); // A missed request without fallback cannot hold forever.

        schedule.reset();
        schedule.armDefault("ep", 70, 501, move(false, true), 8, 400_000_000L);
        schedule.parentSampled(499, "ep", 70);
        schedule.parentSampled(500, "ep", 70);
        yes(schedule.drainEvents().stream().filter(e -> e.status().equals("fallback_armed")).count() == 1);

        schedule.reset();
        var nextDefault = new ClientTimingSchedule.DefaultPlan(703, move(false, true), 8, 400_000_000L);
        schedule.admitPending("ep", 80, 699, 700, 700, now[0] + 1_000_000_000L,
                1, move(true, false), "chained", nextDefault);
        yes(schedule.prepare(700, false).kind() == ClientTimingSchedule.Kind.PENDING);
        schedule.confirmPendingActivation();
        schedule.sampled(700);
        yes(schedule.prepare(703, false).kind() == ClientTimingSchedule.Kind.DEFAULT);
        yes(schedule.prepare(703, false).requestSequenceId() == 80);

        schedule.reset();
        schedule.armDefault("ep", 85, 710, move(false, true), 8, 400_000_000L);
        var badNextDefault = new ClientTimingSchedule.DefaultPlan(713, move(false, true), 8, 400_000_000L);
        schedule.admitPending("ep", 86, 709, 710, 710, now[0] + 1_000_000_000L,
                1, move(true, false), "bad-chain", badNextDefault);
        yes(schedule.prepare(710, false).kind() == ClientTimingSchedule.Kind.PENDING);
        yes("safe_default_activation_mismatch".equals(schedule.preparedPendingDenial(710)));
        schedule.rejectPreparedPending(710);
        yes(schedule.prepare(710, false).kind() == ClientTimingSchedule.Kind.DEFAULT);

        schedule.reset();
        schedule.admitPending("ep", 90, 720, 721, 721, now[0] + 1_000_000_000L,
                1, move(true, false), "revoked", null);
        schedule.drainEvents();
        allowed[0] = false;
        yes(schedule.prepare(721, false).kind() == ClientTimingSchedule.Kind.NEUTRAL);
        yes(schedule.drainEvents().stream().anyMatch(e -> e.status().equals("window_missed")
                && e.requestSequenceId() == 90));
        allowed[0] = true;

        schedule.reset();
        for (int index = 0; index < 64; index++) {
            schedule.admitPending("ep", 100 + index, 800, 900 + index, 900 + index,
                    now[0] + 1_000_000_000L, 1, move(false, false), "bounded", null);
        }
        boolean overflow = false;
        try {
            schedule.admitPending("ep", 164, 800, 964, 964,
                    now[0] + 1_000_000_000L, 1, move(false, false), "overflow", null);
        } catch (IllegalStateException expected) { overflow = true; }
        yes(overflow && schedule.drainEvents().size() == 64);
        System.out.println("CLIENT_TIMING_SCHEDULE_OK");
    }
}
