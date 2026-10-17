import com.mc2p.observation.ClientGuiSession;

public final class ClientGuiSessionTest {
    private static void equal(Object actual, Object expected) {
        if (!java.util.Objects.equals(actual, expected)) throw new AssertionError(actual + " != " + expected);
    }
    public static void main(String[] args) {
        ClientGuiSession session = new ClientGuiSession();
        Object screen = new Object(), handler = new Object();
        equal(session.observe(null, null), null);
        String first = session.observe(screen, handler);
        if (first == null || first.length() > 128) throw new AssertionError("invalid session id");
        equal(session.observe(screen, handler), first);
        equal(session.observe(null, null), null);
        String second = session.observe(screen, handler);
        String third = session.observe(screen, new Object());
        String fourth = session.observe(new Object(), handler);
        session.reset();
        String afterReset = session.observe(screen, handler);
        String anotherClient = new ClientGuiSession().observe(screen, handler);
        if (java.util.Set.of(first, second, third, fourth, afterReset, anotherClient).size() != 6)
            throw new AssertionError("GUI identity reused across lifecycle boundary");
        System.out.println("GUI_SESSION_LIFECYCLE_OK");
    }
}
