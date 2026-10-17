package com.jbrain.tunnelspike

import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.security.MessageDigest
import java.security.cert.X509Certificate
import java.util.Base64

data class RequestHead(val method: String, val target: String, val headers: Map<String, String>)

/** The small slice of HTTP/1.1 and WebSocket the spike's proxy and test server speak. Pure, so
 * the parsing that decides what the proxy forwards is covered by the JVM tests. */
object Http {
    private const val MAX_HEAD = 16 * 1024
    private const val WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    /** One request head, read a byte at a time so nothing past the blank line is consumed: the
     * proxy hands the rest of the stream (a TLS ClientHello) to the upstream untouched. */
    fun readHead(input: InputStream): RequestHead? {
        val buf = ByteArrayOutputStream()
        var tail = 0
        while (buf.size() < MAX_HEAD) {
            val b = input.read()
            if (b < 0) return null
            buf.write(b)
            tail = (tail shl 8) or b
            if (tail == 0x0D0A0D0A) return parseHead(buf.toString(Charsets.ISO_8859_1.name()))
        }
        return null
    }

    fun parseHead(raw: String): RequestHead? {
        val lines = raw.split("\r\n").filter { it.isNotEmpty() }
        val first = lines.firstOrNull()?.split(" ") ?: return null
        if (first.size != 3 || !first[2].startsWith("HTTP/")) return null
        val headers = lines.drop(1).mapNotNull { line ->
            val i = line.indexOf(':')
            if (i <= 0) null else line.substring(0, i).trim().lowercase() to line.substring(i + 1).trim()
        }.toMap()
        return RequestHead(first[0].uppercase(), first[1], headers)
    }

    /** `host:port` of a CONNECT target, or null for anything else (IPv6 literals included: the
     * proxy only ever routes the three named test hosts). */
    fun connectTarget(target: String): Pair<String, Int>? {
        val i = target.lastIndexOf(':')
        if (i <= 0 || target.startsWith("[")) return null
        val port = target.substring(i + 1).toIntOrNull() ?: return null
        return target.substring(0, i).lowercase() to port
    }

    fun basicHeader(user: String, pass: String): String =
        "Basic " + Base64.getEncoder().encodeToString("$user:$pass".toByteArray())

    fun basicMatches(header: String?, user: String, pass: String): Boolean =
        header != null && MessageDigest.isEqual(header.trim().toByteArray(), basicHeader(user, pass).toByteArray())

    fun websocketAccept(key: String): String =
        Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1").digest((key.trim() + WS_GUID).toByteArray()),
        )

    /** The SHA-256 of a certificate's SubjectPublicKeyInfo, base64: the same value a
     * network_security_config `<pin>` carries. */
    fun spki(cert: X509Certificate): String = spki(cert.publicKey.encoded)

    fun spki(subjectPublicKeyInfo: ByteArray): String =
        Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest(subjectPublicKeyInfo))
}

/** Just enough RFC 6455 for one echo: read a client's masked text frame, write an unmasked one. */
object WsFrame {
    fun readText(input: InputStream): String? {
        val b0 = input.read()
        val b1 = input.read()
        if (b0 < 0 || b1 < 0) return null
        if (b0 and 0x0F != 0x1) return null
        var len = (b1 and 0x7F).toLong()
        if (len == 126L) len = ((input.read() shl 8) or input.read()).toLong()
        else if (len == 127L) {
            len = 0
            repeat(8) { len = (len shl 8) or input.read().toLong() }
        }
        if (len > 64 * 1024) return null
        val masked = b1 and 0x80 != 0
        val mask = if (masked) ByteArray(4) { input.read().toByte() } else null
        val payload = ByteArray(len.toInt())
        var off = 0
        while (off < payload.size) {
            val n = input.read(payload, off, payload.size - off)
            if (n < 0) return null
            off += n
        }
        if (mask != null) for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i % 4].toInt()).toByte()
        return String(payload, Charsets.UTF_8)
    }

    fun text(message: String): ByteArray {
        val payload = message.toByteArray(Charsets.UTF_8)
        require(payload.size < 126) { "spike frames stay short" }
        return byteArrayOf(0x81.toByte(), payload.size.toByte()) + payload
    }

    val CLOSE = byteArrayOf(0x88.toByte(), 0)
}
