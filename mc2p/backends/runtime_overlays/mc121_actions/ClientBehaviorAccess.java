package com.mc2p.actions;

/** Access to vanilla use dispatch, not keyboard/mouse callbacks or fabricated packets. */
public interface ClientBehaviorAccess {
    int mc2p$itemUseCooldown();
    void mc2p$useItem();
    int mc2p$attackCooldown();
    boolean mc2p$attack();
    void mc2p$handleBlockBreaking(boolean held);
}
