package com.jbrain.tunnelspike

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.Socket
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.TrustManagerFactory

/** The proxy and the stand-in box on the JVM, end to end, so a phone run that fails points at
 * the WebView and not at the spike's own plumbing. A client trusts only the spike CA (what the
 * network security config does on the phone) and presents a client cert. */
class TunnelPathTest {
    private val raw = File("src/main/res/raw")
    private val password = "spike".toCharArray()
    private lateinit var server: TestServer
    private lateinit var proxy: ConnectProxy

    @Before
    fun start() {
        server = TestServer(File(raw, "spike_leaf.p12").inputStream(), password).apply { start() }
        proxy = ConnectProxy(mapOf("owner.jbrain.home.arpa" to server.port), "spike", "pw", requireAuth = true)
            .apply { start() }
    }

    @After
    fun stop() {
        proxy.stop()
        server.stop()
    }

    @Test
    fun proxyDemandsTheCredential() {
        Socket("127.0.0.1", proxy.port).use { s ->
            s.getOutputStream().write("CONNECT owner.jbrain.home.arpa:443 HTTP/1.1\r\n\r\n".toByteArray())
            assertTrue(statusLine(s).contains("407"))
        }
        assertEquals(1, proxy.challenges)
    }

    @Test
    fun proxyRefusesHostsItDoesNotRoute() {
        Socket("127.0.0.1", proxy.port).use { s ->
            s.getOutputStream().write(
                "CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: ${Http.basicHeader("spike", "pw")}\r\n\r\n".toByteArray(),
            )
            assertTrue(statusLine(s).contains("403"))
        }
    }

    @Test
    fun pingThroughTheProxyReportsTheClientKey() {
        val clientStore = KeyStore.getInstance("PKCS12").apply { File(raw, "spike_fallback.p12").inputStream().use { load(it, password) } }
        val clientCert = clientStore.getCertificate(clientStore.aliases().nextElement()) as X509Certificate
        val ctx = clientContext(clientStore)

        val tcp = Socket("127.0.0.1", proxy.port)
        tcp.getOutputStream().write(
            ("CONNECT owner.jbrain.home.arpa:443 HTTP/1.1\r\n" +
                "Proxy-Authorization: ${Http.basicHeader("spike", "pw")}\r\n\r\n").toByteArray(),
        )
        assertTrue(statusLine(tcp).contains("200"))
        val tls = ctx.socketFactory.createSocket(tcp, "owner.jbrain.home.arpa", 443, true) as SSLSocket
        tls.use {
            tls.startHandshake()
            tls.outputStream.write("GET /api/ping HTTP/1.1\r\nHost: owner.jbrain.home.arpa\r\n\r\n".toByteArray())
            val response = tls.inputStream.readBytes().toString(Charsets.UTF_8)
            assertTrue(response, response.startsWith("HTTP/1.1 200"))
            assertTrue(response, response.contains(Http.spki(clientCert)))
        }
    }

    private fun clientContext(clientStore: KeyStore): SSLContext {
        val ca = File(raw, "spike_ca.pem").inputStream().use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        }
        val trust = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("ca", ca) }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(trust) }
        val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(clientStore, password) }
        return SSLContext.getInstance("TLS").apply { init(kmf.keyManagers, tmf.trustManagers, null) }
    }

    /** The proxy's status line, read without buffering past the head. */
    private fun statusLine(s: Socket): String {
        val head = StringBuilder()
        while (!head.endsWith("\r\n\r\n")) {
            val b = s.getInputStream().read()
            if (b < 0) break
            head.append(b.toChar())
        }
        return head.lineSequence().first()
    }
}
