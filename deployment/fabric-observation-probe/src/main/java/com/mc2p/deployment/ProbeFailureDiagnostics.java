package com.mc2p.deployment;

import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Bounded failure location without messages, paths, payloads or stack dumps. */
public final class ProbeFailureDiagnostics {
    private static final int MAX_CAUSES = 32;

    private ProbeFailureDiagnostics() {}

    public static IllegalStateException transportFailure(Throwable cause) {
        return new IllegalStateException("transport failed", cause);
    }

    public static String format(Throwable error) {
        var chain = new ArrayList<Throwable>();
        Set<Throwable> visited = Collections.newSetFromMap(new IdentityHashMap<>());
        for (Throwable current = error;
                current != null && chain.size() < MAX_CAUSES && visited.add(current);
                current = current.getCause()) {
            chain.add(current);
        }
        String causeType = typeName(chain.get(chain.size() - 1));
        String location = "unknown";
        outer: for (int index = chain.size() - 1; index >= 0; index--) {
            Throwable cause = chain.get(index);
            for (StackTraceElement frame : cause.getStackTrace()) {
                String owner = frame.getClassName();
                String file = frame.getFileName();
                if ((owner.startsWith("com.mc2p.")
                        || owner.startsWith("com.kyhsgeekcode.minecraftenv."))
                        && file != null && file.matches("[A-Za-z_][A-Za-z0-9_]{0,127}\\.java")
                        && frame.getLineNumber() > 0) {
                    causeType = typeName(cause);
                    location = file + ":" + frame.getLineNumber();
                    break outer;
                }
            }
        }
        return typeName(error) + ":cause=" + causeType + ":source=" + location;
    }

    private static String typeName(Throwable error) {
        String name = error.getClass().getSimpleName();
        return name.matches("[A-Za-z_$][A-Za-z0-9_$]{0,127}") ? name : "Throwable";
    }
}
