import com.mc2p.actions.ClientBehaviorInput;
import com.mc2p.actions.ClientRequestGate;

/** Read-only probe of the current input implementation, not a v2 client. */
public final class D122InputPhaseProbe {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        var gate = new ClientRequestGate();
        long[] now = {10};
        var order = new java.util.ArrayList<String>();
        var input = new ClientBehaviorInput(gate, () -> now[0], () -> true,
                () -> now[0], () -> order.add("prepare"), sample -> order.add("sample"));
        yes(gate.admit("phase", 25, 0, 0, 1_000, 1, now[0]) == null);
        input.bindAcceptedRequest("phase", 25);
        input.set(1, 0, false, true, false);
        input.prepareForPlayerTick();
        input.prepareForPlayerTick();
        yes(input.sneaking && !input.jumping && input.sampleCount() == 0);
        yes(gate.leaseActive(now[0]));
        input.tick(input.sneaking, 0.3f);
        yes(input.sampleCount() == 1 && input.leasedSampleCount() == 1);
        yes(!gate.leaseActive(now[0]) && input.movementForward == 0.3f);
        yes(order.equals(java.util.List.of("prepare", "prepare", "sample")));
        // Current code only projects true sneak. Record this fact for D122-2:
        // a successor that clears sneak needs an explicit false projection at HEAD.
        yes(gate.admit("phase", 26, 0, 0, 1_000, 1, now[0]) == null);
        input.beginSnapshot();
        input.bindAcceptedRequest("phase", 26);
        input.set(1, 0, true, false, true);
        input.prepareForPlayerTick();
        boolean staleSneakBeforeSample = input.sneaking;
        yes(input.sampleCount() == 1 && gate.leaseActive(now[0]));
        input.tick(false, 0.3f);
        yes(input.jumping && !input.sneaking && input.sampleCount() == 2);
        yes(!gate.leaseActive(now[0]));
        System.out.println("D122_INPUT_PHASE_OK prepare_consumes=0 sample_consumes=1 stale_sneak_before_sample="
                + staleSneakBeforeSample);
    }
}
