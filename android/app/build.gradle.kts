plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The server base whose /dash the WebView loads. Set per build with
// `-PdashboardBase=https://your-server` (the CI publish-apk job passes the
// DASHBOARD_BASE repo variable); falls back to a placeholder so a bare
// `assembleDebug` still compiles for tests.
val dashboardBase =
    (project.findProperty("dashboardBase") as String?)?.takeIf { it.isNotBlank() }
        ?: "https://example.invalid"

android {
    namespace = "com.jbrain.dashboard"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jbrain.dashboard"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        buildConfigField("String", "DASHBOARD_BASE", "\"$dashboardBase\"")
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // EncryptedSharedPreferences (Keystore-backed) for the device key at rest.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    testImplementation("junit:junit:4.13.2")
    // MockWebServer drives SessionMinter over a real localhost socket on the JVM;
    // org.json gives the unit-test classpath the parser Android ships at runtime.
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.json:json:20240303")
}
