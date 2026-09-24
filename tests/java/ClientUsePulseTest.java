import com.mc2p.actions.ClientUsePulse;

public final class ClientUsePulseTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        boolean[] using = {false}; int[] releases = {0};
        Runnable stop = () -> { using[0] = false; releases[0]++; };
        yes(!ClientUsePulse.dispatch(() -> {}, () -> using[0], stop));
        // No local prediction is not proof that the server did not start using an item.
        yes(releases[0] == 1);
        yes(ClientUsePulse.dispatch(() -> using[0] = true, () -> using[0], stop));
        yes(!using[0] && releases[0] == 2);
        RuntimeException original = new IllegalStateException("dispatch failed after starting use");
        try {
            ClientUsePulse.dispatch(() -> { using[0] = true; throw original; }, () -> using[0], stop);
            throw new AssertionError("failure swallowed");
        } catch (RuntimeException error) { yes(error == original); }
        yes(!using[0] && releases[0] == 3);
        try {
            ClientUsePulse.dispatch(() -> { using[0] = true; throw original; }, () -> using[0],
                                    () -> { throw new IllegalArgumentException("release failed"); });
            throw new AssertionError("failure swallowed");
        } catch (RuntimeException error) { yes(error == original && error.getSuppressed().length == 1); }
        System.out.println("CLIENT_USE_PULSE_OK");
    }
}
