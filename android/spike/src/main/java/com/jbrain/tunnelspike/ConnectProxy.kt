package com.jbrain.tunnelspike

import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.util.Collections
import kotlin.concurrent.thread

/** The in-app proxy the WebView is pointed at. It answers only CONNECT to the test hosts, and
 * demands a per-launch credential when [requireAuth] is set, because any app on the phone can
 * reach a loopback port. */
class ConnectProxy(
    private val routes: Map<String, Int>,
    private val user: String,
    private val pass: String,
    private val requireAuth: Boolean,
) {
    private val server = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
    val port: Int get() = server.localPort
    val targetsSeen: MutableList<String> = Collections.synchronizedList(mutableListOf())
    @Volatile var challenges = 0
        private set
    @Volatile var refused = 0
        private set

    fun start() {
        thread(isDaemon = true, name = "spike-proxy") {
            while (!server.isClosed) {
                val client = try { server.accept() } catch (e: SocketException) { break }
                thread(isDaemon = true) { handle(client) }
            }
        }
    }

    fun stop() = runCatching { server.close() }

    private fun handle(client: Socket) {
        client.use {
            val head = Http.readHead(client.getInputStream()) ?: return
            val out = client.getOutputStream()
            targetsSeen.add("${head.method} ${head.target}")
            if (head.method != "CONNECT") return reply(out, "405 Method Not Allowed")
            if (requireAuth && !Http.basicMatches(head.headers["proxy-authorization"], user, pass)) {
                challenges += 1
                return reply(out, "407 Proxy Authentication Required", "Proxy-Authenticate: Basic realm=\"spike\"\r\n")
            }
            val (host, port) = Http.connectTarget(head.target) ?: return reply(out, "400 Bad Request")
            val upstream = routes[host]
            if (upstream == null || port != 443) {
                refused += 1
                return reply(out, "403 Forbidden")
            }
            Socket("127.0.0.1", upstream).use { up ->
                out.write("HTTP/1.1 200 Connection Established\r\n\r\n".toByteArray())
                out.flush()
                val toUp = thread(isDaemon = true) {
                    pipe(client.getInputStream(), up.getOutputStream())
                    runCatching { up.shutdownOutput() }
                }
                pipe(up.getInputStream(), out)
                runCatching { client.shutdownOutput() }
                toUp.join(30_000)
            }
        }
    }

    private fun reply(out: OutputStream, status: String, extra: String = "") {
        out.write("HTTP/1.1 $status\r\n${extra}Content-Length: 0\r\nConnection: close\r\n\r\n".toByteArray())
        out.flush()
    }

    private fun pipe(from: InputStream, to: OutputStream) {
        val buf = ByteArray(16 * 1024)
        try {
            while (true) {
                val n = from.read(buf)
                if (n < 0) break
                to.write(buf, 0, n)
                to.flush()
            }
        } catch (_: Exception) {
            // Either side closing ends the pipe; the other direction notices on its own.
        }
    }
}
