package com.mc2p.actions;

/**
 * E136: event order of the project's ClientTimingSchedule when a windowed neutral (the D123-2
 * local_not_dispatched exchange, window [35,35]) is staged while an 8-tick sneak default runs.
 * Only calls the project class; one line per admission tick, events in order.
 */
public final class TimingOrderProbe {
    public static void main(String[] args) {
        long[] now = {0};
        var neutral = new ClientTimingSchedule.Movement(0, 0, false, false, false);
        var sneak = new ClientTimingSchedule.Movement(0, 0, false, true, false);
        for (long admit = 27; admit <= 35; admit++) {
            var schedule = new ClientTimingSchedule(() -> now[0], () -> true);
            schedule.armDefault("ep", 25, 27, sneak, 8, 400_000_000L);
            StringBuilder line = new StringBuilder("admitted_at_tick_" + admit + ":");
            for (long tick = 27; tick <= 37; tick++) {
                now[0] = tick * 50_000_000L;
                if (tick == admit) {
                    schedule.admitPending("ep", 26, tick, 35, 35, Long.MAX_VALUE / 2, 1, neutral, null, null);
                }
                var selection = schedule.prepare(tick, false);
                if (selection.kind() == ClientTimingSchedule.Kind.PENDING) schedule.confirmPendingActivation();
                schedule.sampled(tick);
                line.append(" ").append(tick).append("=").append(selection.kind());
                for (var event : schedule.drainEvents())
                    line.append(" [").append(event.status()).append("@").append(event.movementTickId())
                        .append("#").append(event.requestSequenceId()).append("]");
            }
            System.out.println(line);
        }
    }
}
