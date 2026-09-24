import com.mc2p.transport.CraftGroundFrameTransport;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.BooleanSupplier;

public final class CraftGroundFrameTransportTest {
    static void check(boolean condition) { if (!condition) throw new AssertionError(); }
    static void await(BooleanSupplier condition) throws Exception {
        long started = System.nanoTime();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() - started > 2_000_000_000L) throw new AssertionError("condition timeout");
            Thread.sleep(2);
        }
    }
    static int freePort() throws Exception {
        try (ServerSocket reserved = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            return reserved.getLocalPort();
        }
    }
    static Socket connect(CraftGroundFrameTransport transport, int port) throws Exception {
        await(() -> transport.isListening() || transport.failure() != null);
        check(transport.failure() == null);
        Socket peer = new Socket();
        peer.setReceiveBufferSize(1024);
        peer.connect(new InetSocketAddress("127.0.0.1", port), 1000);
        peer.setSoTimeout(2000);
        return peer;
    }
    static void send(Socket peer, byte... bytes) throws Exception {
        peer.getOutputStream().write(bytes); peer.getOutputStream().flush();
    }
    static byte[] frame(CraftGroundFrameTransport transport) throws Exception {
        byte[][] box = new byte[1][];
        await(() -> { box[0] = transport.poll(); return box[0] != null; });
        return box[0];
    }
    static void initialize(CraftGroundFrameTransport transport, Socket peer) throws Exception {
        send(peer, (byte)1, (byte)0, (byte)0, (byte)0, (byte)65);
        check(Arrays.equals(frame(transport), new byte[]{65}));
    }
    static void sealed(CraftGroundFrameTransport transport) throws Exception {
        await(() -> transport.failure() != null && transport.isStopped());
        try { transport.poll(); throw new AssertionError("poll after failure"); }
        catch (IllegalStateException expected) {}
        try { transport.offer(new byte[]{1}); throw new AssertionError("offer after failure"); }
        catch (IllegalStateException expected) {}
        try { transport.start(); throw new AssertionError("start after failure"); }
        catch (IllegalStateException expected) {}
    }
    static void invalidArguments() {
        for (long[] values : new long[][]{{0, 1000, 1000}, {65536, 1000, 1000},
                {8129, 0, 1000}, {8129, 90001, 1000}, {8129, 1000, 0}, {8129, 1000, 30001}}) {
            try {
                new CraftGroundFrameTransport((int)values[0], values[1], values[2]);
                throw new AssertionError("unbounded or invalid configuration");
            } catch (IllegalArgumentException expected) {}
        }
    }
    static void roundTripAndZeroAction() throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 1000)) {
            byte[] outgoing = {7, 8, 9};
            transport.offer(outgoing); outgoing[0] = 99;
            transport.start();
            try (var peer = connect(transport, port)) {
                send(peer, (byte)3, (byte)0); check(transport.poll() == null);
                send(peer, (byte)0, (byte)0, (byte)65); check(transport.poll() == null);
                send(peer, (byte)66, (byte)67);
                check(Arrays.equals(frame(transport), new byte[]{65, 66, 67}));
                check(Arrays.equals(peer.getInputStream().readNBytes(7), new byte[]{3, 0, 0, 0, 7, 8, 9}));
                send(peer, (byte)0, (byte)0, (byte)0, (byte)0);
                check(frame(transport).length == 0); // Empty protobuf action is neutral, not EOF.
                send(peer, (byte)1, (byte)0, (byte)0, (byte)0, (byte)42);
                check(Arrays.equals(frame(transport), new byte[]{42}));
                check(transport.failure() == null);
            }
            sealed(transport);
            transport.close(); transport.close();
        }
        try (ServerSocket rebound = new ServerSocket()) {
            rebound.setReuseAddress(true);
            rebound.bind(new InetSocketAddress("127.0.0.1", port));
        }
    }
    static void rejectedLength(byte[] header, boolean initialized) throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 300)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                if (initialized) initialize(transport, peer);
                send(peer, header);
                sealed(transport);
                check(transport.failure().getMessage().contains("length"));
            }
        }
    }
    static void lengthDomains() throws Exception {
        rejectedLength(new byte[]{0, 0, 0, 0}, false);
        rejectedLength(new byte[]{-1, -1, -1, -1}, false);
        rejectedLength(new byte[]{1, 0, 16, 0}, false); // 1,048,577, before allocation.
        rejectedLength(new byte[]{1, -128, 0, 0}, true); // 32,769, not an initial frame.
        rejectedLength(new byte[]{-1, -1, -1, -1}, true);
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 1000)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                send(peer, (byte)1, (byte)-128, (byte)0, (byte)0);
                peer.getOutputStream().write(new byte[32_769]);
                check(frame(transport).length == 32_769); // Initial environment is allowed above action cap.
            }
        }
    }
    static void partial(boolean headerOnly, boolean eof) throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 150)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                initialize(transport, peer);
                send(peer, headerOnly ? new byte[]{4, 0} : new byte[]{4, 0, 0, 0, 65});
                if (eof) peer.shutdownOutput();
                sealed(transport);
                check(transport.failure().getMessage().contains(eof ? "EOF" : "frame deadline"));
            }
        }
    }
    static void trickleAndIdle() throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 150)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                initialize(transport, peer);
                Thread.sleep(220); // No partial frame: bridge, not transport, owns session idle expiry.
                check(transport.failure() == null && transport.poll() == null);
                send(peer, (byte)100, (byte)0, (byte)0, (byte)0);
                long started = System.nanoTime();
                int sent = 0;
                while (transport.failure() == null && System.nanoTime() - started < 600_000_000L) {
                    try { send(peer, (byte)65); sent++; } catch (IOException closed) { break; }
                    Thread.sleep(30);
                }
                sealed(transport);
                check(sent >= 2 && transport.failure().getMessage().contains("frame deadline"));
            }
        }
    }
    static void bootstrapTimeout(boolean peerPresent) throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 150, 1000)) {
            transport.start();
            if (peerPresent) {
                try (var peer = connect(transport, port)) {
                    send(peer, (byte)100, (byte)0, (byte)0, (byte)0, (byte)65);
                    sealed(transport);
                }
            } else sealed(transport);
            check(transport.failure().getMessage().contains("bootstrap deadline"));
        }
    }
    static void queuesAndWriteDeadline() throws Exception {
        try (var transport = new CraftGroundFrameTransport(freePort(), 1000, 1000)) {
            transport.offer(new byte[]{1});
            try { transport.offer(new byte[]{2}); throw new AssertionError("overwrite queued output"); }
            catch (IllegalStateException expected) {}
            sealed(transport);
        }
        for (int size : new int[]{0, 2_097_153}) {
            try (var transport = new CraftGroundFrameTransport(freePort(), 1000, 1000)) {
                try { transport.offer(new byte[size]); throw new AssertionError("invalid output accepted"); }
                catch (IllegalArgumentException expected) {}
            }
        }
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 1000)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                send(peer, (byte)1, (byte)0, (byte)0, (byte)0, (byte)65,
                           (byte)1, (byte)0, (byte)0, (byte)0, (byte)66);
                sealed(transport);
                check(transport.failure().getMessage().contains("inbound queue"));
            }
        }
        port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 1000, 150)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                initialize(transport, peer);
                transport.offer(new byte[2_097_152]);
                sealed(transport);
                check(transport.failure().getMessage().contains("write deadline"));
            }
        }
    }
    static void localStopAndLifecycle() throws Exception {
        for (boolean accepted : new boolean[]{false, true}) {
            int port = freePort();
            try (var transport = new CraftGroundFrameTransport(port, 1000, 1000)) {
                transport.start();
                try (Socket peer = accepted ? connect(transport, port) : null) {
                    if (peer != null) send(peer, (byte)3);
                    transport.requestStop();
                    transport.close(); transport.close();
                    check(transport.isStopped() && transport.failure() == null);
                    try { transport.start(); throw new AssertionError("restart accepted"); }
                    catch (IllegalStateException expected) {}
                }
            }
        }
        try (var transport = new CraftGroundFrameTransport(freePort(), 1000, 1000)) {
            var entered = new CountDownLatch(1);
            var done = new CountDownLatch(1);
            var error = new AtomicReference<Throwable>();
            Thread closer = new Thread(() -> {
                entered.countDown();
                try { transport.close(); } catch (Throwable failed) { error.set(failed); }
                finally { done.countDown(); }
            });
            boolean crossed;
            synchronized (transport) {
                closer.start(); check(entered.await(1, TimeUnit.SECONDS));
                crossed = done.await(100, TimeUnit.MILLISECONDS);
                if (!crossed) transport.start();
            }
            closer.join(2000);
            check(!crossed && !closer.isAlive() && error.get() == null && transport.isStopped());
        }
    }
    static Object field(Object instance, String name) throws Exception {
        var member = instance.getClass().getDeclaredField(name);
        member.setAccessible(true);
        return member.get(instance);
    }
    static void finalIoCrossesDeadline(boolean writing) throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 2000, 300)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                initialize(transport, peer);
                var selector = (java.nio.channels.Selector) field(transport, "selector");
                var channel = selector.keys().stream().map(key -> key.channel())
                        .filter(value -> value instanceof java.nio.channels.SocketChannel).findFirst().orElseThrow();
                var ioLock = (ReentrantLock) field(channel, writing ? "writeLock" : "readLock");
                var worker = (Thread) field(transport, "worker");
                ioLock.lock();
                try {
                    if (writing) transport.offer(new byte[]{42});
                    else send(peer, (byte)0, (byte)0, (byte)0, (byte)0);
                    await(() -> ioLock.hasQueuedThread(worker));
                    Thread.sleep(400); // Cross the whole-frame deadline inside the final actual I/O call.
                } finally { ioLock.unlock(); }
                sealed(transport);
                check(transport.failure().getMessage().contains(writing ? "write deadline" : "read frame deadline"));
            }
        }
    }
    static void invalidOutgoingSealsSession() throws Exception {
        for (byte[] payload : new byte[][]{null, new byte[0], new byte[2_097_153]}) {
            int port = freePort();
            try (var transport = new CraftGroundFrameTransport(port, 2000, 1000)) {
                transport.start();
                try (var peer = connect(transport, port)) {
                    initialize(transport, peer);
                    IllegalArgumentException rejected = null;
                    try { transport.offer(payload); throw new AssertionError("invalid output accepted"); }
                    catch (IllegalArgumentException expected) { rejected = expected; }
                    sealed(transport);
                    check(transport.failure() == rejected);
                }
            }
        }
    }
    static void callerDoesNotWaitForIoLock(boolean overflow) throws Exception {
        int port = freePort();
        try (var transport = new CraftGroundFrameTransport(port, 2000, 2000)) {
            transport.start();
            try (var peer = connect(transport, port)) {
                initialize(transport, peer);
                var selector = (java.nio.channels.Selector) field(transport, "selector");
                var channel = selector.keys().stream().map(key -> key.channel())
                        .filter(value -> value instanceof java.nio.channels.SocketChannel).findFirst().orElseThrow();
                var writeLock = (ReentrantLock) field(channel, "writeLock");
                var worker = (Thread) field(transport, "worker");
                var done = new CountDownLatch(1);
                var error = new AtomicReference<Throwable>();
                Thread caller = new Thread(() -> {
                    try {
                        if (overflow) {
                            try { transport.offer(new byte[]{3}); throw new AssertionError("overflow accepted"); }
                            catch (IllegalStateException expected) {}
                        } else transport.requestStop();
                    } catch (Throwable unexpected) { error.set(unexpected); }
                    finally { done.countDown(); }
                });
                boolean returned;
                writeLock.lock();
                try {
                    transport.offer(new byte[]{1});
                    await(() -> worker.getState() == Thread.State.WAITING);
                    if (overflow) transport.offer(new byte[]{2});
                    caller.start();
                    returned = done.await(200, TimeUnit.MILLISECONDS);
                } finally { writeLock.unlock(); }
                caller.join(2000);
                if (!returned) throw new AssertionError("client caller waited for worker I/O lock");
                check(!caller.isAlive() && error.get() == null);
                await(transport::isStopped);
                check(overflow ? transport.failure().getMessage().contains("outbound queue") : transport.failure() == null);
            }
        }
    }
    static void occupiedPortFailsWithoutClosingItsOwner() throws Exception {
        try (var owner = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"));
             var transport = new CraftGroundFrameTransport(owner.getLocalPort(), 1000, 1000)) {
            owner.setSoTimeout(1000);
            transport.start();
            sealed(transport);
            check(!owner.isClosed());
            try (var connector = new Socket("127.0.0.1", owner.getLocalPort()); var accepted = owner.accept()) {
                check(accepted.isConnected());
            }
        }
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 1) {
            switch (args[0]) {
                case "late-zero" -> finalIoCrossesDeadline(false);
                case "late-write" -> finalIoCrossesDeadline(true);
                case "invalid-output" -> invalidOutgoingSealsSession();
                default -> throw new IllegalArgumentException("unknown test case");
            }
            System.out.println("CRAFTGROUND_FRAME_TRANSPORT_OK");
            return;
        }
        invalidArguments(); roundTripAndZeroAction(); lengthDomains();
        for (boolean header : new boolean[]{false, true})
            for (boolean eof : new boolean[]{false, true}) partial(header, eof);
        trickleAndIdle(); bootstrapTimeout(false); bootstrapTimeout(true);
        queuesAndWriteDeadline(); localStopAndLifecycle();
        callerDoesNotWaitForIoLock(false); callerDoesNotWaitForIoLock(true);
        occupiedPortFailsWithoutClosingItsOwner();
        System.out.println("CRAFTGROUND_FRAME_TRANSPORT_OK");
    }
}
