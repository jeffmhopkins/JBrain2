package com.jbrain.tunnelspike

import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.SocketException
import java.security.KeyStore
import java.security.cert.X509Certificate
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager
import kotlin.concurrent.thread

/** Stands in for Caddy's owner site: TLS with a client certificate demanded on every
 * connection, the test page, a fetch target, SSE and a WebSocket echo. Loopback only. */
class TestServer(p12: InputStream, password: CharArray) {
    private val socket: SSLServerSocket
    @Volatile var lastClientSpki: String = "none"
        private set
    @Volatile var handshakes = 0
        private set
    @Volatile var lastError: String = ""
        private set

    init {
        val ks = KeyStore.getInstance("PKCS12").apply { load(p12, password) }
        val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(ks, password) }
        val ctx = SSLContext.getInstance("TLS").apply { init(kmf.keyManagers, arrayOf(AnyClientCert), null) }
        socket = (ctx.serverSocketFactory.createServerSocket(0, 50, InetAddress.getByName("127.0.0.1")) as SSLServerSocket)
            .apply { needClientAuth = true }
    }

    val port: Int get() = socket.localPort

    fun start() {
        thread(isDaemon = true, name = "spike-server") {
            while (!socket.isClosed) {
                val conn = try { socket.accept() as SSLSocket } catch (e: SocketException) { break }
                thread(isDaemon = true) { serve(conn) }
            }
        }
    }

    fun stop() = runCatching { socket.close() }

    private fun serve(conn: SSLSocket) {
        conn.use {
            try {
                conn.soTimeout = 15_000
                conn.startHandshake()
                handshakes += 1
                (conn.session.peerCertificates.firstOrNull() as? X509Certificate)?.let { lastClientSpki = Http.spki(it) }
                val input = conn.inputStream
                val out = conn.outputStream
                val head = Http.readHead(input) ?: return
                when (head.target.substringBefore('?')) {
                    "/" -> respond(out, "text/html; charset=utf-8", TestPage.HTML)
                    "/sw.js" -> respond(out, "application/javascript", TestPage.SERVICE_WORKER)
                    "/api/ping" -> respond(out, "application/json", """{"ok":true,"clientSpki":"$lastClientSpki"}""")
                    "/sse" -> sse(out)
                    "/ws" -> websocket(head, input, out)
                    else -> respond(out, "text/plain", "not found", status = "404 Not Found")
                }
            } catch (e: Exception) {
                lastError = e.toString()
            }
        }
    }

    private fun respond(out: OutputStream, type: String, body: String, status: String = "200 OK") {
        val bytes = body.toByteArray(Charsets.UTF_8)
        // CORS open so the owner page can probe the pin-mismatch host, which only answers if the
        // WebView ignored its pin-set.
        val head = "HTTP/1.1 $status\r\nContent-Type: $type\r\nContent-Length: ${bytes.size}\r\n" +
            "Cache-Control: no-store\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n"
        out.write(head.toByteArray(Charsets.ISO_8859_1))
        out.write(bytes)
        out.flush()
    }

    private fun sse(out: OutputStream) {
        out.write("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".toByteArray())
        out.flush()
        for (i in 1..3) {
            out.write("data: $i\n\n".toByteArray())
            out.flush()
            Thread.sleep(200)
        }
    }

    private fun websocket(head: RequestHead, input: InputStream, out: OutputStream) {
        val key = head.headers["sec-websocket-key"] ?: return
        out.write(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
                "Sec-WebSocket-Accept: ${Http.websocketAccept(key)}\r\n\r\n").toByteArray(),
        )
        out.flush()
        val msg = WsFrame.readText(input) ?: return
        out.write(WsFrame.text("echo:$msg"))
        out.write(WsFrame.CLOSE)
        out.flush()
    }

    /** The server asks for a client cert and records whichever one comes; whether the WebView
     * sends the Keystore key's is what the report compares. */
    private object AnyClientCert : X509TrustManager {
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) = Unit
        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) =
            throw java.security.cert.CertificateException("server side only")
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }
}
