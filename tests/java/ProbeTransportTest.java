import com.mc2p.deployment.ProbeTransport;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Arrays;
import java.util.function.BooleanSupplier;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.ReentrantLock;

public class ProbeTransportTest {
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    static void await(BooleanSupplier condition) throws Exception {
        long end = System.nanoTime() + 2_000_000_000L;
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > end) throw new AssertionError("condition deadline");
            Thread.sleep(2);
        }
    }
    static ServerSocket server() throws Exception {
        ServerSocket server = new ServerSocket();
        server.setReceiveBufferSize(1024);
        server.bind(new java.net.InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0));
        server.setSoTimeout(2000);
        return server;
    }
    static byte[] awaitFrame(ProbeTransport transport) throws Exception {
        byte[][] box = new byte[1][];
        await(() -> { box[0] = transport.poll(); return box[0] != null; });
        return box[0];
    }
    static void invalidArguments() {
        for (int port : new int[]{-1, 0, 65536}) {
            try { new ProbeTransport(port, 100); throw new AssertionError(); }
            catch (IllegalArgumentException expected) {}
        }
        for (long timeout : new long[]{0, -1, 30001}) {
            try { new ProbeTransport(8129, timeout); throw new AssertionError(); }
            catch (IllegalArgumentException expected) {}
        }
    }
    static void roundTrip() throws Exception {
        int releasedPort;
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 1000)) {
            releasedPort = server.getLocalPort();
            transport.start();
            try (Socket peer = server.accept()) {
                peer.setSoTimeout(2000);
                var output = new DataOutputStream(peer.getOutputStream());
                output.write(new byte[]{0, 0, 0, 3, 65}); output.flush();
                check(transport.poll() == null);
                output.write(new byte[]{66, 67}); output.flush();
                check(Arrays.equals(awaitFrame(transport), new byte[]{65, 66, 67}));
                byte[] outgoing = new byte[]{7, 8, 9};
                transport.offer(outgoing); outgoing[0] = 99;
                var input = new DataInputStream(peer.getInputStream());
                check(input.readInt() == 3);
                check(Arrays.equals(input.readNBytes(3), new byte[]{7, 8, 9}));
                check(transport.failure() == null);
                transport.close(); transport.close();
                check(transport.isStopped());
            }
        }
        try (var rebound = new ServerSocket()) {
            rebound.setReuseAddress(true);
            rebound.bind(new java.net.InetSocketAddress("127.0.0.1", releasedPort));
        }
    }
    static void rejectedLength(int length) throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 300)) {
            transport.start();
            try (var peer = server.accept()) {
                var output = new DataOutputStream(peer.getOutputStream());
                output.writeInt(length); output.flush();
                await(() -> transport.failure() != null && transport.isStopped());
                try { transport.poll(); throw new AssertionError(); }
                catch (IllegalStateException expected) {}
            }
        }
    }
    static void partialRead(boolean eof) throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 150)) {
            transport.start();
            try (var peer = server.accept()) {
                peer.getOutputStream().write(new byte[]{0, 0, 0, 4, 65});
                peer.getOutputStream().flush();
                if (eof) peer.shutdownOutput();
                await(() -> transport.failure() != null && transport.isStopped());
            }
        }
    }
    static void inboundOverflow() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 1000)) {
            transport.start();
            try (var peer = server.accept()) {
                peer.getOutputStream().write(new byte[]{0,0,0,1,65,0,0,0,1,66});
                peer.getOutputStream().flush();
                await(() -> transport.failure() != null && transport.isStopped());
                check(transport.failure().getMessage().contains("inbound queue"));
            }
        }
    }
    static void outboundBoundsAndOverflow() {
        try (var transport = new ProbeTransport(8129, 1000)) {
            for (byte[] value : new byte[][]{new byte[0], new byte[1_114_113]}) {
                try { transport.offer(value); throw new AssertionError(); }
                catch (IllegalArgumentException expected) {}
            }
            transport.offer(new byte[]{1});
            try { transport.offer(new byte[]{2}); throw new AssertionError(); }
            catch (IllegalStateException expected) {}
            check(transport.failure() != null);
            check(transport.isStopped());
        }
    }
    static void blockedWriteExpires() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 150)) {
            transport.start();
            try (var peer = server.accept()) {
                transport.offer(new byte[1_114_112]);
                await(() -> transport.failure() != null && transport.isStopped());
                check(transport.failure().getMessage().contains("write deadline"));
            }
        }
    }
    static void localCancel() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 1000)) {
            transport.start();
            try (var peer = server.accept()) {
                peer.getOutputStream().write(0); peer.getOutputStream().flush();
                transport.close();
                check(transport.isStopped());
                check(transport.failure() == null);
            }
        }
    }
    static Object field(Object object, String name) throws Exception {
        var field = object.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(object);
    }
    static void overflowDoesNotWaitForSocketLock() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 2000)) {
            transport.start();
            try (var peer = server.accept()) {
                var selector = (java.nio.channels.Selector) field(transport, "selector");
                var channel = selector.keys().iterator().next().channel();
                var writeLock = (ReentrantLock) field(channel, "writeLock");
                var worker = (Thread) field(transport, "worker");
                var done = new CountDownLatch(1);
                var error = new AtomicReference<Throwable>();
                Thread caller = new Thread(() -> {
                    try { transport.offer(new byte[]{3}); error.set(new AssertionError("overflow accepted")); }
                    catch (IllegalStateException expected) {}
                    catch (Throwable unexpected) { error.set(unexpected); }
                    finally { done.countDown(); }
                });
                boolean returned;
                writeLock.lock();
                try {
                    transport.offer(new byte[]{1});
                    await(() -> worker.getState() == Thread.State.WAITING);
                    transport.offer(new byte[]{2});
                    caller.start();
                    returned = done.await(200, TimeUnit.MILLISECONDS);
                } finally { writeLock.unlock(); }
                caller.join(2000);
                check(!caller.isAlive());
                if (!returned) throw new AssertionError("offer waited for a socket I/O lock");
                check(error.get() == null);
                check(transport.failure().getMessage().contains("outbound queue"));
            }
        }
    }
    static void closeCannotCrossInFlightStart() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 1000)) {
            var entered = new CountDownLatch(1);
            var done = new CountDownLatch(1);
            var error = new AtomicReference<Throwable>();
            Thread closer = new Thread(() -> {
                entered.countDown();
                try { transport.close(); }
                catch (Throwable unexpected) { error.set(unexpected); }
                finally { done.countDown(); }
            });
            boolean crossed;
            // Hold the same short monitor as start(), forcing an in-flight lifecycle transition.
            synchronized (transport) {
                closer.start();
                check(entered.await(1, TimeUnit.SECONDS));
                crossed = done.await(100, TimeUnit.MILLISECONDS);
                if (!crossed) transport.start();
            }
            closer.join(2000);
            check(!closer.isAlive());
            if (crossed) throw new AssertionError("close crossed the start lifecycle lock");
            check(error.get() == null && transport.isStopped());
            try { transport.start(); throw new AssertionError("start after close accepted"); }
            catch (IllegalStateException expected) {}
        }
    }
    static void trickleCannotExtendFrameDeadline() throws Exception {
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 150)) {
            transport.start();
            try (var peer = server.accept()) {
                var output = new DataOutputStream(peer.getOutputStream());
                output.writeInt(1000); output.flush();
                long started = System.nanoTime();
                int sent = 0;
                while (transport.failure() == null && System.nanoTime() - started < 600_000_000L) {
                    try { output.writeByte(65); output.flush(); sent++; }
                    catch (java.io.IOException closed) { break; }
                    Thread.sleep(30);
                }
                check(sent >= 2);
                check(transport.failure() != null);
                check(transport.failure().getMessage().contains("read frame deadline"));
                await(transport::isStopped);
            }
        }
    }
    static void clientStopDoesNotJoinIoWorker() throws Exception {
        java.lang.reflect.Method requestStop;
        try { requestStop = ProbeTransport.class.getMethod("requestStop"); }
        catch (NoSuchMethodException missing) { throw new AssertionError("client-thread stop API missing", missing); }
        try (var server = server(); var transport = new ProbeTransport(server.getLocalPort(), 2000)) {
            transport.start();
            try (var peer = server.accept()) {
                var selector = (java.nio.channels.Selector) field(transport, "selector");
                var channel = selector.keys().iterator().next().channel();
                var writeLock = (ReentrantLock) field(channel, "writeLock");
                var worker = (Thread) field(transport, "worker");
                var done = new CountDownLatch(1);
                var error = new AtomicReference<Throwable>();
                Thread caller = new Thread(() -> {
                    try { requestStop.invoke(transport); }
                    catch (Throwable unexpected) { error.set(unexpected); }
                    finally { done.countDown(); }
                });
                boolean returned;
                writeLock.lock();
                try {
                    transport.offer(new byte[]{1});
                    await(() -> worker.getState() == Thread.State.WAITING);
                    caller.start();
                    returned = done.await(200, TimeUnit.MILLISECONDS);
                } finally { writeLock.unlock(); }
                caller.join(2000);
                if (!returned) throw new AssertionError("client stop waited for I/O worker");
                check(error.get() == null);
                await(transport::isStopped);
                check(transport.failure() == null);
            }
        }
    }
    public static void main(String[] args) throws Exception {
        if (args.length != 0 && args[0].equals("lifecycle")) {
            closeCannotCrossInFlightStart();
            return;
        }
        invalidArguments(); roundTrip();
        for (int length : new int[]{0, -1, 16385}) rejectedLength(length);
        partialRead(true); partialRead(false); inboundOverflow(); outboundBoundsAndOverflow();
        blockedWriteExpires(); localCancel();
        overflowDoesNotWaitForSocketLock(); closeCannotCrossInFlightStart();
        trickleCannotExtendFrameDeadline();
        clientStopDoesNotJoinIoWorker();
        System.out.println("DEPLOYMENT_TRANSPORT_OK");
    }
}
