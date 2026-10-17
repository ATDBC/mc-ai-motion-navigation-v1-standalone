import com.kyhsgeekcode.minecraftenv.LockstepClientSimulationGate;

public final class LockstepClientSimulationGateTest {
    private static void expect(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        expect(LockstepClientSimulationGate.allowPlayerTick());
        LockstepClientSimulationGate.beginCycle(false);
        expect(!LockstepClientSimulationGate.allowPlayerTick());
        LockstepClientSimulationGate.admitAction();
        expect(LockstepClientSimulationGate.allowPlayerTick());
        for (int i = 0; i < 5; i++) {
            LockstepClientSimulationGate.beginCycle(false);
            expect(!LockstepClientSimulationGate.allowPlayerTick());
        }
        LockstepClientSimulationGate.beginCycle(true); // reset/initialization progresses normally
        expect(LockstepClientSimulationGate.allowPlayerTick());
        Throwable[] errors = {null};
        Thread other = new Thread(() -> {
            try { LockstepClientSimulationGate.admitAction(); } catch (Throwable error) { errors[0] = error; }
        });
        other.start(); other.join();
        expect(errors[0] instanceof IllegalStateException);
        System.out.println("LOCKSTEP_PLAYER_SIMULATION_GATE_OK");
    }
}
