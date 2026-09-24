package com.kyhsgeekcode.minecraftenv

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.util.UUID

internal object LockstepTrace {
    const val FILE_NAME = "mc2p-lockstep.jsonl"
    private val sessionId = UUID.randomUUID().toString()
    private val path = Path.of(FILE_NAME)

    @Synchronized
    fun record(
        event: String,
        generation: Long,
        serverWorldTime: Long? = null,
        clientWorldTime: Long? = null,
    ) {
        val serverValue = serverWorldTime?.toString() ?: "null"
        val clientValue = clientWorldTime?.toString() ?: "null"
        val line =
            "{\"schema_version\":\"mc2p.lockstep-trace.v0\"," +
                "\"session_id\":\"$sessionId\"," +
                "\"event\":\"$event\"," +
                "\"generation\":$generation," +
                "\"server_world_time\":$serverValue," +
                "\"client_world_time\":$clientValue}"
        Files.writeString(
            path,
            "$line\n",
            StandardCharsets.UTF_8,
            StandardOpenOption.CREATE,
            StandardOpenOption.WRITE,
            StandardOpenOption.APPEND,
        )
    }
}
