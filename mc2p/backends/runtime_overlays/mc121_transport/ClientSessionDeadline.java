package com.kyhsgeekcode.minecraftenv;

import java.util.Objects;
import java.util.function.LongSupplier;

/** Monotonic client-thread deadline; an expired session can never be renewed. */
public final class ClientSessionDeadline {
    private static final long RESET_NANOS = 90_000_000_000L;
    private static final long ACTION_NANOS = 30_000_000_000L;
    private final Thread owner = Thread.currentThread();
    private final LongSupplier clock;
    private long deadline;
    private boolean expired;

    public ClientSessionDeadline() { this(System::nanoTime); }

    public ClientSessionDeadline(LongSupplier clock) {
        this.clock = Objects.requireNonNull(clock);
        deadline = clock.getAsLong() + RESET_NANOS;
    }

    private long checkedNow() {
        if (Thread.currentThread() != owner) throw new IllegalStateException("deadline outside client thread");
        long now = clock.getAsLong();
        if (now - deadline >= 0) expired = true;
        if (expired) throw new IllegalStateException("client session deadline exceeded");
        return now;
    }

    public void check() { checkedNow(); }
    public void beginReset() { deadline = checkedNow() + RESET_NANOS; }
    public void observationOffered() { deadline = checkedNow() + ACTION_NANOS; }
}
