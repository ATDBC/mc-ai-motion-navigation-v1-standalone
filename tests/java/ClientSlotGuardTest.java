import com.mc2p.actions.ClientSlotGuard;

public final class ClientSlotGuardTest {
    private static void expect(String expected, String actual) {
        if (!java.util.Objects.equals(expected, actual))
            throw new AssertionError("expected " + expected + ", got " + actual);
    }
    public static void main(String[] args) {
        expect(null, ClientSlotGuard.validate("gui-1", 0, 2, 9, "gui-1", 0, 2, 46, true));
        expect("stale_gui_session", ClientSlotGuard.validate("gui-1", 0, 2, 9, null, 0, 2, 46, true));
        // A reopened personal inventory reuses syncId=0, but must never reuse its session.
        expect("stale_gui_session", ClientSlotGuard.validate("gui-1", 0, 2, 9, "gui-2", 0, 2, 46, true));
        expect("stale_handler", ClientSlotGuard.validate("gui-1", 2, 2, 9, "gui-1", 3, 2, 46, true));
        expect("stale_revision", ClientSlotGuard.validate("gui-1", 0, 1, 9, "gui-1", 0, 2, 46, true));
        expect("invalid_slot", ClientSlotGuard.validate("gui-1", 0, 2, 46, "gui-1", 0, 2, 46, false));
        expect("invalid_slot", ClientSlotGuard.validate("gui-1", 0, 2, -999, "gui-1", 0, 2, 46, false));
        expect("disabled_slot", ClientSlotGuard.validate("gui-1", 0, 2, 9, "gui-1", 0, 2, 46, false));
        System.out.println("CLIENT_SLOT_GUARD_OK");
    }
}
