package com.jbrain.tunnelspike

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import javax.security.auth.x500.X500Principal

/** The phone's non-exportable device key, made the way the owner app would make it. The
 * Keystore mints a self-signed certificate for it, which is all a TLS client cert needs here. */
class DeviceKey(val alias: String, val wantStrongBox: Boolean) {
    val privateKey: PrivateKey
    val certificate: X509Certificate
    val strongBox: Boolean
    val securityLevel: String

    init {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        ks.deleteEntry(alias)
        strongBox = wantStrongBox && Build.VERSION.SDK_INT >= 28 && runCatching { generate(true) }.isSuccess
        if (!strongBox) generate(false)
        privateKey = ks.getKey(alias, null) as PrivateKey
        certificate = ks.getCertificate(alias) as X509Certificate
        securityLevel = describe(privateKey)
    }

    val spki: String get() = Http.spki(certificate)

    /** Average milliseconds for one SHA256withECDSA signature: one TLS handshake's cost. */
    fun signMillis(rounds: Int = 5): Double {
        val data = ByteArray(64) { it.toByte() }
        val start = System.nanoTime()
        repeat(rounds) {
            Signature.getInstance("SHA256withECDSA").apply { initSign(privateKey); update(data); sign() }
        }
        return (System.nanoTime() - start) / 1e6 / rounds
    }

    private fun generate(strong: Boolean) {
        // StrongBox implements SHA-256 only; a TEE key also allows NONE in case the WebView
        // signs a pre-hashed digest.
        val digests = if (strong) arrayOf(KeyProperties.DIGEST_SHA256)
        else arrayOf(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_NONE)
        val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(*digests)
            .setCertificateSubject(X500Principal("CN=tunnel-spike-device"))
            .apply { if (strong && Build.VERSION.SDK_INT >= 28) setIsStrongBoxBacked(true) }
            .build()
        KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
            initialize(spec)
            generateKeyPair()
        }
    }

    private fun describe(key: PrivateKey): String = runCatching {
        val info = KeyFactory.getInstance(key.algorithm, "AndroidKeyStore").getKeySpec(key, KeyInfo::class.java)
        if (Build.VERSION.SDK_INT >= 31) {
            when (info.securityLevel) {
                KeyProperties.SECURITY_LEVEL_STRONGBOX -> "strongbox"
                KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT -> "tee"
                KeyProperties.SECURITY_LEVEL_SOFTWARE -> "software"
                else -> "level ${info.securityLevel}"
            }
        } else {
            @Suppress("DEPRECATION")
            if (info.isInsideSecureHardware) "secure hardware" else "software"
        }
    }.getOrElse { "unknown: $it" }
}
