package com.mc2p.actions;

/** Owner-thread lifecycle only. The vanilla driver supplies target checks and normal interactions. */
public final class ClientMiningLoop {
    public interface Driver {
        boolean valid();
        void start();
        void progress();
        void cancel();
    }

    private final Thread owner = Thread.currentThread();
    private Driver driver;
    private boolean started;

    private void requireOwner() {
        if (Thread.currentThread() != owner) throw new IllegalStateException("mining outside client thread");
    }

    public boolean active() { requireOwner(); return driver != null; }

    public void request(Driver next) {
        requireOwner();
        if (next == null) throw new IllegalArgumentException("missing mining driver");
        stop();
        driver = next;
    }

    public void advance(boolean leaseActive) {
        requireOwner();
        if (driver == null) return;
        try {
            if (!leaseActive || !driver.valid()) { stop(); return; }
            if (!started) { started = true; driver.start(); }
            // An instant break must not continue into the newly exposed block.
            if (!driver.valid()) { stop(); return; }
            driver.progress();
        } catch (RuntimeException | Error error) {
            try { stop(); }
            catch (RuntimeException | Error cleanup) { if (cleanup != error) error.addSuppressed(cleanup); }
            throw error;
        }
    }

    /** START expiry/target check, without starting or advancing a block. */
    public void expire(boolean leaseActive) {
        requireOwner();
        if (driver == null) return;
        try { if (!leaseActive || !driver.valid()) stop(); }
        catch (RuntimeException | Error error) {
            try { stop(); }
            catch (RuntimeException | Error cleanup) { if (cleanup != error) error.addSuppressed(cleanup); }
            throw error;
        }
    }

    /** Reset the other owned state even when vanilla cancellation throws. */
    public void reset(Runnable releaseState) {
        requireOwner();
        try { stop(); }
        catch (RuntimeException | Error error) {
            try { releaseState.run(); }
            catch (RuntimeException | Error cleanup) { if (cleanup != error) error.addSuppressed(cleanup); }
            throw error;
        }
        releaseState.run();
    }

    public void stop() {
        requireOwner();
        Driver previous = driver;
        driver = null;
        started = false;
        if (previous != null) previous.cancel();
    }
}
