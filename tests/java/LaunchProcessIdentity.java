public final class LaunchProcessIdentity {
    public static void main(String[] args) {
        System.out.println(ProcessHandle.current().pid());
    }
}
