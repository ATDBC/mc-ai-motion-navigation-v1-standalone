package com.mc2p.diagnostics;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.function.Consumer;

/** Single-writer diagnostic files; no actor or Minecraft state access. */
public final class ClientTimeSegmentWriter implements Consumer<String>, AutoCloseable {
    @FunctionalInterface interface StreamFactory { OutputStream open(Path path) throws IOException; }
    private final Path directory;
    private final int maxBytes;
    private final StreamFactory factory;
    private OutputStream stream, manifest;
    private MessageDigest segmentHash = digest(), manifestHash = digest();
    private long index, totalRecords, totalBytes, segmentRecords, segmentBytes, diskChecked;
    private boolean closed;
    private Throwable failure;

    public ClientTimeSegmentWriter(Path directory, int maxSegmentBytes) throws IOException {
        this(directory, maxSegmentBytes, path -> Files.newOutputStream(path, StandardOpenOption.CREATE_NEW));
    }
    ClientTimeSegmentWriter(Path directory, int maxSegmentBytes, StreamFactory factory) throws IOException {
        if (maxSegmentBytes <= 0) throw new IllegalArgumentException("invalid segment byte limit");
        this.directory = safe(directory);
        this.maxBytes = maxSegmentBytes;
        this.factory = factory;
        Files.createDirectories(this.directory.getParent());
        Files.createDirectory(this.directory);
        try {
            checkDisk(true);
            manifest = factory.open(safe(this.directory.resolve("manifest.jsonl")));
        } catch (IOException | RuntimeException | Error error) { fail(error); throw error; }
    }
    private static MessageDigest digest() {
        try { return MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException impossible) { throw new IllegalStateException(impossible); }
    }
    private static Path safe(Path path) throws IOException {
        path = path.toAbsolutePath().normalize();
        for (Path part = path; part != null; part = part.getParent()) {
            if (!Files.exists(part, LinkOption.NOFOLLOW_LINKS)) continue;
            var attrs = Files.readAttributes(part, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            if (attrs.isSymbolicLink() || attrs.isOther()) throw new IOException("symlink/reparse evidence path");
        }
        return path;
    }
    private void checkDisk(boolean force) throws IOException {
        long now = System.nanoTime();
        if (force || now-diskChecked >= 1_000_000_000L) {
            if (Files.getFileStore(directory).getUsableSpace() < 512L*1024*1024)
                throw new IOException("evidence_disk_low: less than 512 MiB free");
            diskChecked = now;
        }
    }
    private String filename() { return String.format(java.util.Locale.ROOT, "segment-%08d.jsonl", index); }
    private static byte[] encode(JsonObject value) { return (value.toString()+"\n").getBytes(StandardCharsets.UTF_8); }
    @Override public synchronized void accept(String line) {
        if (closed) throw new IllegalStateException("diagnostic stream is closed or failed", failure);
        if (line == null || line.indexOf('\n') >= 0 || line.indexOf('\r') >= 0
                || !JsonParser.parseString(line).isJsonObject()) throw new IllegalArgumentException("invalid diagnostic JSONL record");
        byte[] bytes = (line+"\n").getBytes(StandardCharsets.UTF_8);
        if (bytes.length > Math.min(maxBytes, 8*1024*1024)) throw new IllegalArgumentException("diagnostic record exceeds byte limit");
        try {
            boolean rotating = segmentBytes+bytes.length > maxBytes;
            checkDisk(rotating || stream == null);
            if (rotating) finishSegment();
            if (stream == null) {
                if (index > 99_999_999) throw new IOException("diagnostic segment index exhausted");
                stream = factory.open(safe(directory.resolve(filename())));
            }
            stream.write(bytes);
            stream.flush();
            segmentHash.update(bytes);
            segmentBytes += bytes.length;
            totalBytes += bytes.length;
            segmentRecords++;
            totalRecords++;
        } catch (IOException error) { fail(error); throw new UncheckedIOException(error); }
        catch (RuntimeException | Error error) { fail(error); throw error; }
    }
    private void finishSegment() throws IOException {
        if (stream == null) return;
        stream.flush();
        stream.close();
        stream = null;
        var row = new JsonObject();
        row.addProperty("schema_version", "mc2p.segment.v1");
        row.addProperty("index", index);
        row.addProperty("filename", filename());
        row.addProperty("byte_count", segmentBytes);
        row.addProperty("record_count", segmentRecords);
        row.addProperty("first_record_ordinal", totalRecords-segmentRecords);
        row.addProperty("last_record_ordinal", totalRecords-1);
        row.addProperty("sha256", HexFormat.of().formatHex(segmentHash.digest()));
        byte[] bytes = encode(row);
        manifest.write(bytes);
        manifest.flush();
        manifestHash.update(bytes);
        index++;
        segmentRecords = segmentBytes = 0;
        segmentHash = digest();
    }
    private void fail(Throwable error) {
        if (failure == null) failure = error;
        else if (failure != error) failure.addSuppressed(error);
        closed = true;
        for (OutputStream item : new OutputStream[]{stream, manifest}) {
            if (item != null) try { item.close(); }
            catch (IOException | RuntimeException | Error cleanup) {
                if (cleanup != failure) failure.addSuppressed(cleanup);
            }
        }
        stream = manifest = null;
    }
    @Override public synchronized void close() throws IOException {
        if (closed) return;
        try {
            finishSegment();
            manifest.flush();
            manifest.close();
            manifest = null;
            var row = new JsonObject();
            row.addProperty("schema_version", "mc2p.segment-complete.v1");
            row.addProperty("segment_count", index);
            row.addProperty("record_count", totalRecords);
            row.addProperty("byte_count", totalBytes);
            row.addProperty("manifest_sha256", HexFormat.of().formatHex(manifestHash.digest()));
            Path temporary = safe(directory.resolve("complete.pending"));
            try (var output = factory.open(temporary)) { output.write(encode(row)); output.flush(); }
            Files.move(temporary, safe(directory.resolve("complete.json")));
            closed = true;
        } catch (IOException | RuntimeException | Error error) { fail(error); throw error; }
    }
}
