plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "pl.przewijak.artykuly"
    compileSdk = 36

    defaultConfig {
        applicationId = "pl.przewijak.artykuly"
        minSdk = 24
        targetSdk = 36
        versionCode = 15300
        versionName = "15.3"

        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    sourceSets.getByName("main") {
        java.srcDir("src/main/java")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            install("requests>=2.32,<3")
            install("beautifulsoup4>=4.12,<5")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
    }
}

dependencies {
    implementation("androidx.documentfile:documentfile:1.1.0")
}
