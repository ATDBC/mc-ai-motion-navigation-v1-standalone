import com.google.protobuf.ByteString;
import com.google.protobuf.UnknownFieldSet;
import com.kyhsgeekcode.minecraftenv.NonBlockingMessageIO;
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace.ActionSpaceMessageV2;
import com.kyhsgeekcode.minecraftenv.proto.InitialEnvironment.InitialEnvironmentMessage;
import com.kyhsgeekcode.minecraftenv.proto.ObservationSpace.ObservationSpaceMessage;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

/** Actual Kotlin pump, upstream generated protobuf and loopback sockets; no game stubs. */
public final class NonBlockingMessageIOTest {
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    static void await(BooleanSupplier condition) throws Exception {
        long started = System.nanoTime();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() - started > 2_000_000_000L) throw new AssertionError("condition timeout");
            Thread.sleep(2);
        }
    }
    static <T> T poll(Supplier<T> source) throws Exception {
        var value = new AtomicReference<T>();
        await(() -> { value.set(source.get()); return value.get() != null; });
        return value.get();
    }
    static int freePort() throws Exception {
        try (var listener = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            return listener.getLocalPort();
        }
    }
    static Socket connect(NonBlockingMessageIO pump, int port) throws Exception {
        await(pump::isListening);
        var socket = new Socket();
        socket.connect(new InetSocketAddress("127.0.0.1", port), 1000);
        socket.setSoTimeout(2000);
        return socket;
    }
    static void send(Socket socket, byte[] data) throws Exception {
        var output = socket.getOutputStream();
        int size = data.length;
        output.write(new byte[]{(byte)size, (byte)(size >> 8), (byte)(size >> 16), (byte)(size >> 24)});
        output.write(data); output.flush();
    }
    static void initialize(NonBlockingMessageIO pump, Socket socket) throws Exception {
        var initial = InitialEnvironmentMessage.newBuilder().setImageSizeX(64).build();
        send(socket, initial.toByteArray());
        check(poll(pump::pollInitialEnvironment).equals(initial));
    }
    static UnknownFieldSet.Field field(String value) {
        return UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFromUtf8(value)).build();
    }
    static void protobufRoundTrip() throws Exception {
        int port = freePort();
        try (var pump = new NonBlockingMessageIO(port, false, 2000, 1000)) {
            pump.start();
            check(pump.pollInitialEnvironment() == null);
            try (var peer = connect(pump, port)) {
                initialize(pump, peer);
                check(pump.pollAction() == null);
                var request = ActionSpaceMessageV2.newBuilder().setUnknownFields(
                        UnknownFieldSet.newBuilder().addField(50001, field("action-v1-wire")).build()).build();
                send(peer, request.toByteArray());
                var decoded = poll(pump::pollAction);
                check(decoded.equals(request) && decoded.getAllFields().isEmpty());
                var observation = ObservationSpaceMessage.newBuilder().setUnknownFields(
                        UnknownFieldSet.newBuilder().addField(50000, field("observation-v2-wire"))
                                .addField(50002, field("receipt-wire")).build()).build();
                pump.offerObservation(observation);
                byte[] header = peer.getInputStream().readNBytes(4);
                check(header.length == 4);
                int length = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN).getInt();
                check(length == observation.getSerializedSize());
                check(ObservationSpaceMessage.parseFrom(peer.getInputStream().readNBytes(length)).equals(observation));
                send(peer, new byte[0]);
                check(poll(pump::pollAction).equals(ActionSpaceMessageV2.getDefaultInstance()));
                pump.requestStop();
                pump.close(); pump.close();
                check(pump.isStopped() && pump.failure() == null);
            }
        }
    }
    static void sealed(NonBlockingMessageIO pump) throws Exception {
        check(pump.failure() != null);
        await(pump::isStopped);
        try { pump.pollAction(); throw new AssertionError("poll after failure"); }
        catch (IllegalStateException expected) {}
        try { pump.offerObservation(ObservationSpaceMessage.getDefaultInstance()); throw new AssertionError("offer after failure"); }
        catch (IllegalStateException expected) {}
        try { pump.start(); throw new AssertionError("restart after failure"); }
        catch (IllegalStateException expected) {}
    }
    static void malformedProto(boolean initial) throws Exception {
        int port = freePort();
        try (var pump = new NonBlockingMessageIO(port, false, 2000, 1000)) {
            pump.start();
            try (var peer = connect(pump, port)) {
                if (!initial) initialize(pump, peer);
                send(peer, new byte[]{(byte)0xff});
                try {
                    poll(initial ? pump::pollInitialEnvironment : pump::pollAction);
                    throw new AssertionError("invalid protobuf accepted");
                } catch (IllegalStateException expected) {
                    check(expected.getCause() instanceof com.google.protobuf.InvalidProtocolBufferException);
                    check(pump.failure() == expected.getCause());
                }
                sealed(pump);
            }
        }
    }
    static void phaseAndThreadGuards() throws Exception {
        for (int operation = 0; operation < 4; operation++) {
            int port = freePort();
            try (var pump = new NonBlockingMessageIO(port, false, 2000, 1000)) {
                pump.start();
                try (var peer = connect(pump, port)) {
                    if (operation > 1) initialize(pump, peer);
                    if (operation == 3) {
                        var error = new AtomicReference<Throwable>();
                        Thread other = new Thread(() -> {
                            try { pump.pollAction(); } catch (Throwable failed) { error.set(failed); }
                        });
                        other.start(); other.join(1000);
                        check(!other.isAlive() && error.get() instanceof IllegalStateException);
                    } else {
                        try {
                            if (operation == 0) pump.pollAction();
                            else if (operation == 1) pump.offerObservation(ObservationSpaceMessage.getDefaultInstance());
                            else pump.pollInitialEnvironment();
                            throw new AssertionError("invalid phase accepted");
                        } catch (IllegalStateException expected) {}
                    }
                    sealed(pump);
                }
            }
        }
        int port = freePort();
        try {
            new NonBlockingMessageIO(port, true, 2000, 1000);
            throw new AssertionError("shared memory fallback accepted");
        } catch (IllegalArgumentException expected) {}
        try (var owner = new ServerSocket(port, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            check(owner.isBound()); // Unsupported configuration opened no listener.
        }
    }
    static void originalTransportFailureIsStable() throws Exception {
        int port = freePort();
        try (var pump = new NonBlockingMessageIO(port, false, 2000, 1000)) {
            pump.start();
            try (var peer = connect(pump, port)) {
                initialize(pump, peer);
                peer.shutdownOutput();
                await(() -> pump.failure() != null);
                Throwable original = pump.failure();
                check(original instanceof java.io.EOFException);
                try { pump.pollAction(); throw new AssertionError("EOF stream still usable"); }
                catch (IllegalStateException expected) {}
                if (pump.failure() != original) throw new AssertionError("failure root identity changed after poll");
                sealed(pump);
                check(pump.failure() == original);
            }
        }
    }
    public static void main(String[] args) throws Exception {
        protobufRoundTrip(); malformedProto(true); malformedProto(false); phaseAndThreadGuards();
        originalTransportFailureIsStable();
        System.out.println("NONBLOCKING_MESSAGE_IO_OK");
    }
}
