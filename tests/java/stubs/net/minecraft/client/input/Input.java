package net.minecraft.client.input;

/**
 * Test-only surface of Minecraft 1.21's Input class used by
 * ClientBehaviorInput. The Fabric build remains the compatibility gate for
 * the real mapped Minecraft API.
 */
public class Input {
    public float movementForward;
    public float movementSideways;
    public boolean pressingForward;
    public boolean pressingBack;
    public boolean pressingLeft;
    public boolean pressingRight;
    public boolean jumping;
    public boolean sneaking;

    public void tick(boolean slowDown, float slowDownFactor) {}
}
