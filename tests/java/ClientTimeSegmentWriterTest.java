package com.mc2p.diagnostics;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.UncheckedIOException;

public final class ClientTimeSegmentWriterTest {
    private static void yes(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        var writer = new ClientTimeSegmentWriter(root.resolve("good"), 128);
        for (int i = 0; i < 1000; i++) writer.accept("{\"number\":" + i + ",\"text\":\"汉字\"}");
        writer.close();
        writer.close();
        try { writer.accept("{}"); throw new AssertionError("closed write accepted"); }
        catch (IllegalStateException expected) {}
        yes(Files.isRegularFile(root.resolve("good/complete.json")));
        try { new ClientTimeSegmentWriter(root.resolve("good"), 128); throw new AssertionError("overwrite accepted"); }
        catch (IOException expected) {}

        var failed = new ClientTimeSegmentWriter(root.resolve("half"), 128, path -> {
            var stream = Files.newOutputStream(path, java.nio.file.StandardOpenOption.CREATE_NEW);
            if (!path.getFileName().toString().startsWith("segment-")) return stream;
            return new FilterOutputStream(stream) {
                @Override public void write(byte[] bytes) throws IOException {
                    out.write(bytes, 0, 3);
                    throw new IOException("injected half write");
                }
                @Override public void close() throws IOException {
                    out.close();
                    throw new IOException("injected close failure");
                }
            };
        });
        try { failed.accept("{\"number\":1}"); throw new AssertionError("half write accepted"); }
        catch (UncheckedIOException expected) {
            yes(expected.getCause().getMessage().equals("injected half write"));
            yes(expected.getCause().getSuppressed().length == 1);
        }
        failed.close();
        yes(!Files.exists(root.resolve("half/complete.json")));
        yes(Files.size(root.resolve("half/segment-00000000.jsonl")) == 3);

        var sealFailure = new ClientTimeSegmentWriter(root.resolve("seal"), 128);
        sealFailure.accept("{}");
        Files.writeString(root.resolve("seal/complete.pending"), "existing", StandardCharsets.UTF_8);
        try { sealFailure.close(); throw new AssertionError("completion overwrite accepted"); }
        catch (IOException expected) {}
        yes(!Files.exists(root.resolve("seal/complete.json")));
        System.out.println("CLIENT_TIME_SEGMENTS_OK");
    }
}
