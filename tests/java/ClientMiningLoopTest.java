import com.mc2p.actions.ClientMiningLoop;
import java.util.ArrayList;
import java.util.List;

public final class ClientMiningLoopTest {
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    private static final class Driver implements ClientMiningLoop.Driver {
        final List<String> events = new ArrayList<>();
        boolean visible = true, instant, failProgress, failCancel, failValid;
        final RuntimeException progressFailure = new RuntimeException("progress");
        public boolean valid() { if (failValid) throw progressFailure; return visible; }
        public void start() { events.add("start"); if (instant) visible = false; }
        public void progress() { events.add("progress"); if (failProgress) throw progressFailure; }
        public void cancel() { events.add("cancel"); if (failCancel) throw new RuntimeException("cancel"); }
    }
    public static void main(String[] args) throws Exception {
        ClientMiningLoop loop = new ClientMiningLoop();
        Driver driver = new Driver();
        loop.request(driver);
        loop.advance(true); loop.advance(true);
        check(driver.events.equals(List.of("start", "progress", "progress")));
        loop.advance(false); loop.advance(true); loop.stop();
        check(driver.events.equals(List.of("start", "progress", "progress", "cancel")));
        check(!loop.active());
        driver = new Driver(); driver.instant = true;
        loop.request(driver); loop.advance(true);
        check(driver.events.equals(List.of("start", "cancel"))); // Never mine the next block.
        driver = new Driver(); driver.visible = false;
        loop.request(driver); loop.advance(true);
        check(driver.events.equals(List.of("cancel")));
        driver = new Driver(); driver.failProgress = driver.failCancel = true;
        loop.request(driver);
        try { loop.advance(true); throw new AssertionError("missing failure"); }
        catch (RuntimeException error) {
            check(error == driver.progressFailure);
            check(error.getSuppressed().length == 1);
        }
        check(!loop.active());
        loop.advance(true); // Failure cannot revive an old request.
        Driver recovery = new Driver();
        loop.request(recovery); loop.advance(true); loop.stop();
        check(recovery.events.equals(List.of("start", "progress", "cancel")));
        Driver badTarget = new Driver();
        loop.request(badTarget); loop.advance(true);
        badTarget.failValid = badTarget.failCancel = true;
        try { loop.expire(true); throw new AssertionError("missing target failure"); }
        catch (RuntimeException error) {
            check(error == badTarget.progressFailure && error.getSuppressed().length == 1);
        }
        check(!loop.active());
        Driver badReset = new Driver(); badReset.failCancel = true;
        loop.request(badReset); loop.advance(true);
        int[] released = {0};
        try { loop.reset(() -> released[0]++); throw new AssertionError("missing cancel failure"); }
        catch (RuntimeException error) { check(error.getMessage().equals("cancel")); }
        check(released[0] == 1 && !loop.active());
        Throwable[] failure = {null};
        Thread other = new Thread(() -> {
            try { loop.advance(true); } catch (Throwable error) { failure[0] = error; }
        });
        other.start(); other.join(1000);
        check(!other.isAlive() && failure[0] instanceof IllegalStateException);
        System.out.println("CLIENT_MINING_LOOP_OK");
    }
}
