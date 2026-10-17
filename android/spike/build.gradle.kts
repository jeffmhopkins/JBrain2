plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// OWNER_TUNNEL_PLAN Wave S0: a throwaway test APK that proves, on a real phone, the WebView
// behaviours the owner tunnel rests on. It carries its own test "box" (a loopback TLS server)
// so it needs no server changes; its own applicationId so it never touches the real app.
android {
    namespace = "com.jbrain.tunnelspike"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jbrain.tunnelspike"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
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
    // ProxyController (reverse bypass) and the service-worker client hook.
    implementation("androidx.webkit:webkit:1.12.1")
    testImplementation("junit:junit:4.13.2")
}
