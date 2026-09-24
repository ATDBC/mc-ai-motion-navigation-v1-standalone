import com.kyhsgeekcode.minecraftenv.ClientSessionDeadline;

public final class ClientSessionDeadlineTest {
    static void expired(Runnable action) {
        try { action.run(); throw new AssertionError("expired session revived"); }
        catch (IllegalStateException expected) {}
    }
    public static void main(String[] args) {
        long[] clock = {0};
        var deadline = new ClientSessionDeadline(() -> clock[0]);
        clock[0] = 89_999_999_999L;
        deadline.check(); // START was still before bootstrap expiration.
        clock[0] = 90_000_000_000L;
        expired(deadline::check); // END/offer crossed expiration.
        expired(deadline::observationOffered);
        expired(deadline::beginReset);
        clock[0] = 100_000_000_000L;
        var live = new ClientSessionDeadline(() -> clock[0]);
        live.observationOffered();
        clock[0] = 129_999_999_999L;
        live.check();
        clock[0]++;
        expired(live::check); expired(live::observationOffered);
        var reset = new ClientSessionDeadline(() -> clock[0]);
        reset.observationOffered();
        clock[0] += 10_000_000_000L;
        reset.beginReset();
        clock[0] += 89_999_999_999L;
        reset.check();
        clock[0]++;
        expired(reset::check);
        System.out.println("CLIENT_SESSION_DEADLINE_OK");
    }
}
