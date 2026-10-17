package com.mc2p.actions;

import java.util.function.BooleanSupplier;

/** One-shot dispatch cannot lend an unimplemented held-use state to a blocking transport. */
public final class ClientUsePulse {
    private ClientUsePulse() {}
    public static boolean dispatch(Runnable dispatch, BooleanSupplier startedUse, Runnable release) {
        boolean locallyStarted;
        try {
            dispatch.run();
            locallyStarted = startedUse.getAsBoolean();
        } catch (RuntimeException | Error failure) {
            try { release.run(); }
            catch (RuntimeException | Error cleanup) { failure.addSuppressed(cleanup); }
            throw failure;
        }
        // interactItem sends a request even when local prediction does not start use.
        // Release through the normal manager in all cases; local false is not server truth.
        release.run();
        return locallyStarted;
    }
}
