package com.mc2p.deployment;

import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.StandardSocketOptions;
import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.SocketChannel;
import java.util.concurrent.ArrayBlockingQueue;

/** Off-thread, bounded loopback framing. The client thread never waits for network I/O. */
public final class ProbeTransport implements AutoCloseable {
    private static final int MAX_INBOUND = 16_384, MAX_OUTBOUND = 1_114_112;
    private final int port;
    private final long timeoutNs;
    private final ArrayBlockingQueue<byte[]> inbound = new ArrayBlockingQueue<>(1);
    private final ArrayBlockingQueue<byte[]> outbound = new ArrayBlockingQueue<>(1);
    private volatile boolean closed;
    private volatile Throwable failure;
    private volatile Thread worker;
    private volatile Selector selector;

    public ProbeTransport(int port, long timeoutMillis) {
        if (port < 1 || port > 65535 || timeoutMillis < 1 || timeoutMillis > 30000)
            throw new IllegalArgumentException("invalid loopback transport port/timeout");
        this.port = port;
        this.timeoutNs = timeoutMillis * 1_000_000L;
    }

    public synchronized void start() {
        requireOpen();
        if (worker != null) throw new IllegalStateException("transport already started");
        worker = new Thread(this::run, "mc2p-deployment-transport");
        worker.setDaemon(true);
        worker.start();
    }

    /** May queue the initial payload before start; never silently replace queued data. */
    public void offer(byte[] payload) {
        requireOpen();
        if (payload == null || payload.length == 0 || payload.length > MAX_OUTBOUND)
            throw new IllegalArgumentException("invalid outbound frame length");
        if (!outbound.offer(payload.clone())) {
            fail(new IOException("outbound queue overflow"));
            throw new IllegalStateException("outbound queue overflow", failure);
        }
        Selector active = selector;
        if (active != null) active.wakeup();
    }

    public byte[] poll() {
        requireOpen();
        return inbound.poll();
    }

    public Throwable failure() { return failure; }
    public boolean isStopped() { return worker == null || !worker.isAlive(); }

    /** Client-tick cancellation only publishes intent; the worker owns socket cleanup. */
    public void requestStop() {
        closed = true;
        wakeup();
    }

    private void requireOpen() {
        if (closed) throw new IllegalStateException("transport closed", failure);
    }

    private void run() {
        try (var socket = SocketChannel.open(); var events = Selector.open()) {
            selector = events;
            if (closed) return;
            socket.configureBlocking(false);
            socket.setOption(StandardSocketOptions.TCP_NODELAY, true);
            socket.setOption(StandardSocketOptions.SO_SNDBUF, 16384);
            socket.connect(new InetSocketAddress(InetAddress.getByAddress(new byte[]{127, 0, 0, 1}), port));
            SelectionKey key = socket.register(events, SelectionKey.OP_CONNECT);
            long connectStarted = System.nanoTime(), readStarted = 0, writeStarted = 0;
            ByteBuffer header = ByteBuffer.allocate(4), body = null, write = null;
            while (!closed) {
                long now = System.nanoTime();
                if (!socket.isConnected()) {
                    if (now - connectStarted >= timeoutNs) throw new IOException("connect deadline exceeded");
                    if (!socket.finishConnect()) {
                        events.select(20);
                        events.selectedKeys().clear();
                        continue;
                    }
                }
                if (write == null) {
                    byte[] payload = outbound.poll();
                    if (payload != null) {
                        write = ByteBuffer.allocate(4 + payload.length).putInt(payload.length).put(payload).flip();
                        writeStarted = now;
                    }
                }
                if (write != null) {
                    if (now - writeStarted >= timeoutNs) throw new IOException("write deadline exceeded");
                    socket.write(write);
                    if (!write.hasRemaining()) write = null;
                }
                if (readStarted != 0 && now - readStarted >= timeoutNs)
                    throw new IOException("read frame deadline exceeded");
                ByteBuffer destination = body == null ? header : body;
                int count = socket.read(destination);
                if (count < 0) throw new EOFException("peer closed deployment transport");
                if (count > 0 && readStarted == 0) readStarted = now;
                if (!destination.hasRemaining()) {
                    if (body == null) {
                        int length = header.flip().getInt();
                        if (length <= 0 || length > MAX_INBOUND) throw new IOException("invalid inbound frame length");
                        body = ByteBuffer.allocate(length);
                    } else {
                        if (!inbound.offer(body.array())) throw new IOException("inbound queue overflow");
                        body = null;
                        header.clear();
                        readStarted = 0;
                    }
                }
                key.interestOps(SelectionKey.OP_READ | (write == null ? 0 : SelectionKey.OP_WRITE));
                events.select(20);
                events.selectedKeys().clear();
            }
        } catch (Throwable error) {
            if (!closed) fail(error);
        } finally {
            closed = true;
        }
    }

    private synchronized void fail(Throwable error) {
        if (failure == null) failure = error;
        closed = true;
        wakeup();
    }

    private void wakeup() {
        Selector active = selector;
        if (active != null && active.isOpen()) active.wakeup();
    }

    /** Owner-side bounded join. Do not call from a client tick; use requestStop there. */
    @Override public void close() {
        Thread active;
        // Publish closure atomically with start(), but never hold this lock during join.
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
        inbound.clear();
        outbound.clear();
    }
}
