package com.mc2p.transport;

import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.StandardSocketOptions;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.ClosedSelectorException;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.util.concurrent.ArrayBlockingQueue;

/** Single-session LE framing; only the worker touches sockets, never the client tick caller. */
public final class CraftGroundFrameTransport implements AutoCloseable {
    private static final int MAX_INITIAL = 1_048_576, MAX_ACTION = 32_768, MAX_OBSERVATION = 2_097_152;
    private final int port;
    private final long bootstrapBudgetNs, frameBudgetNs;
    private final ArrayBlockingQueue<byte[]> inbound = new ArrayBlockingQueue<>(1);
    private final ArrayBlockingQueue<byte[]> outbound = new ArrayBlockingQueue<>(1);
    private volatile boolean closed, listening;
    private volatile Throwable failure;
    private volatile Thread worker;
    private volatile Selector selector;

    public CraftGroundFrameTransport(int port, long bootstrapTimeoutMillis, long frameTimeoutMillis) {
        if (port < 1 || port > 65535 || bootstrapTimeoutMillis < 1 || bootstrapTimeoutMillis > 90000
                || frameTimeoutMillis < 1 || frameTimeoutMillis > 30000)
            throw new IllegalArgumentException("invalid loopback port or bounded timeout");
        this.port = port;
        bootstrapBudgetNs = bootstrapTimeoutMillis * 1_000_000L;
        frameBudgetNs = frameTimeoutMillis * 1_000_000L;
    }

    public synchronized void start() {
        requireOpen();
        if (worker != null) throw new IllegalStateException("transport already started");
        worker = new Thread(this::run, "mc2p-craftground-transport");
        worker.setDaemon(true);
        worker.start();
    }

    public byte[] poll() {
        requireOpen();
        byte[] payload = inbound.poll();
        requireOpen();
        return payload;
    }

    public void offer(byte[] payload) {
        requireOpen();
        if (payload == null || payload.length < 1 || payload.length > MAX_OBSERVATION) {
            var error = new IllegalArgumentException("invalid outgoing observation length");
            fail(error);
            throw error;
        }
        if (!outbound.offer(payload.clone())) {
            fail(new IOException("outbound queue overflow"));
            throw new IllegalStateException("outbound queue overflow", failure);
        }
        requireOpen();
        wakeup();
    }

    public Throwable failure() { return failure; }
    public boolean isListening() { return listening; }
    public boolean isStopped() { return worker == null || !worker.isAlive(); }

    /** Stop intent is safe at the client boundary; only the worker closes its socket. */
    public void requestStop() {
        closed = true;
        wakeup();
    }

    private void requireOpen() {
        if (closed) throw new IllegalStateException("transport closed", failure);
    }

    private synchronized void fail(Throwable error) {
        if (failure == null) failure = error;
        closed = true;
        wakeup();
    }

    private void wakeup() {
        Selector active = selector;
        if (active != null) {
            try { active.wakeup(); }
            catch (ClosedSelectorException alreadyClosed) { /* Worker has finished cleanup. */ }
        }
    }

    private void publish(byte[] payload) throws IOException {
        if (!inbound.offer(payload)) throw new IOException("inbound queue overflow");
    }

    private void run() {
        long bootstrapStarted = System.nanoTime();
        SocketChannel peer = null;
        try (var server = ServerSocketChannel.open(); var events = Selector.open()) {
            selector = events;
            if (closed) return;
            server.configureBlocking(false);
            server.bind(new InetSocketAddress(InetAddress.getByAddress(new byte[]{127, 0, 0, 1}), port));
            server.register(events, SelectionKey.OP_ACCEPT);
            listening = true;
            SelectionKey peerKey = null;
            boolean initialReceived = false;
            long readStarted = 0, writeStarted = 0;
            ByteBuffer header = ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN);
            ByteBuffer body = null, write = null;
            while (!closed) {
                long now = System.nanoTime();
                if (!initialReceived && now - bootstrapStarted >= bootstrapBudgetNs)
                    throw new IOException("bootstrap deadline exceeded");
                if (peer == null) {
                    peer = server.accept();
                    if (peer == null) {
                        events.select(20); events.selectedKeys().clear();
                        continue;
                    }
                    server.close(); listening = false;
                    peer.configureBlocking(false);
                    peer.setOption(StandardSocketOptions.TCP_NODELAY, true);
                    peer.setOption(StandardSocketOptions.SO_SNDBUF, 16384);
                    peerKey = peer.register(events, SelectionKey.OP_READ);
                }
                if (write == null) {
                    byte[] payload = outbound.poll();
                    if (payload != null) {
                        write = ByteBuffer.allocate(4 + payload.length).order(ByteOrder.LITTLE_ENDIAN)
                                .putInt(payload.length).put(payload).flip();
                        writeStarted = now;
                    }
                }
                if (write != null) {
                    if (now - writeStarted >= frameBudgetNs) throw new IOException("write deadline exceeded");
                    peer.write(write);
                    if (System.nanoTime() - writeStarted >= frameBudgetNs)
                        throw new IOException("write deadline exceeded");
                    if (!write.hasRemaining()) write = null;
                }
                now = System.nanoTime();
                if (initialReceived && readStarted != 0 && now - readStarted >= frameBudgetNs)
                    throw new IOException("read frame deadline exceeded");
                ByteBuffer destination = body == null ? header : body;
                int count = peer.read(destination);
                if (count < 0) throw new EOFException("EOF from controller");
                if (count > 0 && readStarted == 0) readStarted = now;
                if (!destination.hasRemaining()) {
                    // Check every completed header/body, including a zero-byte neutral action.
                    long completed = System.nanoTime();
                    if (!initialReceived && completed - bootstrapStarted >= bootstrapBudgetNs)
                        throw new IOException("bootstrap deadline exceeded");
                    if (initialReceived && completed - readStarted >= frameBudgetNs)
                        throw new IOException("read frame deadline exceeded");
                    if (body == null) {
                        int length = header.flip().getInt();
                        if (length < (initialReceived ? 0 : 1) || length > (initialReceived ? MAX_ACTION : MAX_INITIAL))
                            throw new IOException("invalid inbound frame length");
                        if (length == 0) {
                            publish(new byte[0]);
                            header.clear(); readStarted = 0;
                        } else body = ByteBuffer.allocate(length);
                    } else {
                        publish(body.array());
                        initialReceived = true;
                        body = null; header.clear(); readStarted = 0;
                    }
                }
                peerKey.interestOps(SelectionKey.OP_READ | (write == null ? 0 : SelectionKey.OP_WRITE));
                events.select(20); events.selectedKeys().clear();
            }
        } catch (Throwable error) {
            if (!closed) fail(error);
        } finally {
            if (peer != null) {
                try { peer.close(); }
                catch (IOException error) { fail(error); }
            }
            listening = false;
            closed = true;
            inbound.clear(); outbound.clear();
        }
    }

    /** Bounded owner-side join, forbidden on a client tick; use requestStop there. */
    @Override public void close() {
        Thread active;
        synchronized (this) {
            closed = true;
            active = worker;
        }
        wakeup();
        if (active != null && active != Thread.currentThread()) {
            try { active.join(1000); }
            catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("interrupted transport cleanup", error);
            }
            if (active.isAlive()) throw new IllegalStateException("transport worker did not stop");
        }
        inbound.clear(); outbound.clear();
    }
}
