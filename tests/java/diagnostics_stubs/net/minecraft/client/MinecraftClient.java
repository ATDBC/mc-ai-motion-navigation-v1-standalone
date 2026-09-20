package net.minecraft.client;

/** Test-only boundary for sidecar lifecycle; not a Minecraft simulation. */
public class MinecraftClient {
    private static final MinecraftClient INSTANCE = new MinecraftClient();
    public net.minecraft.client.world.ClientWorld world = new net.minecraft.client.world.ClientWorld();
    public Player player = new Player();
    public enum Pose { STANDING, CROUCHING }
    public static class Velocity {
        public final double x,y,z;
        public Velocity(double x,double y,double z) { this.x=x;this.y=y;this.z=z; }
    }
    public static class Player {
        public boolean sprinting,sneaking,onGround=true,horizontalCollision,verticalCollision;
        public double x=1.5,y=64,z=2.5;
        public float yaw=15,pitch=-10;
        public Pose pose=Pose.STANDING;
        public Velocity velocity=new Velocity(0,0,0);
        public boolean isSprinting() { return sprinting; }
        public boolean isSneaking() { return sneaking; }
        public boolean isOnGround() { return onGround; }
        public Pose getPose() { return pose; }
        public Velocity getVelocity() { return velocity; }
        public double getX() { return x; }
        public double getY() { return y; }
        public double getZ() { return z; }
        public float getYaw() { return yaw; }
        public float getPitch() { return pitch; }
    }
    public static MinecraftClient getInstance() { return INSTANCE; }
    public boolean isOnThread() { return true; }
}
