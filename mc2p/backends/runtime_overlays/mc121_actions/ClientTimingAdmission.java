package com.mc2p.actions;

/** Consumes one request identity before checking D122 timing semantics. Owner thread only. */
public final class ClientTimingAdmission {
    public record Result(String denial, long deadlineNs) {}
    public enum RejectionDisposition { NONE, HOLD_INCUMBENT, HARD_RELEASE }

    private ClientTimingAdmission() {}

    public static RejectionDisposition preStageRejectionDisposition(
            ClientActionRequest action, boolean hardRelease) {
        if (action.firstEffectWindow() == null) return RejectionDisposition.NONE;
        return hardRelease ? RejectionDisposition.HARD_RELEASE : RejectionDisposition.HOLD_INCUMBENT;
    }

    public static Result admit(ClientRequestGate gate, ClientActionRequest action,
                               long latestObservation, long budget, long now,
                               long nextMovementTick) {
        String gateDenied = gate.admitPending(action.episode(), action.sequence(), action.observation(),
                latestObservation, budget, action.ticks());
        if (gateDenied != null) return new Result(gateDenied, 0);
        String semanticDenied = action.timingAdmissionDenial(nextMovementTick);
        if (semanticDenied != null) return new Result(semanticDenied, 0);
        long deadlineNs = now > Long.MAX_VALUE - budget ? Long.MAX_VALUE : now + budget;
        if (action.firstEffectWindow() == null
                && !gate.activatePending(action.episode(), action.sequence(), deadlineNs,
                                         action.ticks(), now))
            return new Result("timing_activation_rejected", 0);
        return new Result(null, deadlineNs);
    }
}
