package pl.przewijak.artykuly;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

public class MainActivity extends Activity {
    private static final int REQ_TREE = 4107;
    private static final int REQ_NOTIFY = 4108;
    private WebView webView;
    private PyObject mobile;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("przewijak", MODE_PRIVATE);

        Intent svc = new Intent(this, CollectorService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);

        if (Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
        }

        Python py = Python.getInstance();
        mobile = py.getModule("mobile_entry");
        int port = mobile.callAttr("start_server").toInt();

        String uri = prefs.getString("saf_uri", "");
        String label = prefs.getString("saf_label", "");
        if (!uri.isEmpty()) {
            try { mobile.callAttr("restore_saf_target", uri, label); } catch (Exception ignored) {}
        }

        webView = new WebView(this);
        setContentView(webView);
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setAllowFileAccess(false);
        ws.setAllowContentAccess(true);
        ws.setMediaPlaybackRequiresUserGesture(true);
        if (Build.VERSION.SDK_INT >= 21) {
            ws.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        }
        webView.addJavascriptInterface(new AndroidBridge(), "AndroidBridge");
        webView.setWebViewClient(new WebViewClient());
        webView.loadUrl("http://127.0.0.1:" + port + "/");
    }

    public class AndroidBridge {
        @JavascriptInterface
        public void chooseFolder() {
            runOnUiThread(() -> {
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION |
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION |
                        Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION |
                        Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                startActivityForResult(intent, REQ_TREE);
            });
        }

        @JavascriptInterface
        public void syncNow() {
            runOnUiThread(() -> {
                boolean ok = SafSync.syncAll(MainActivity.this);
                Toast.makeText(MainActivity.this, ok ? "Synchronizacja zakończona" : "Brak wybranego folderu", Toast.LENGTH_SHORT).show();
            });
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_TREE || resultCode != RESULT_OK || data == null || data.getData() == null) return;

        Uri uri = data.getData();
        int flags = data.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        try {
            getContentResolver().takePersistableUriPermission(uri, flags);
        } catch (Exception ignored) {}

        String label = uri.getLastPathSegment();
        if (label == null || label.isEmpty()) label = "wybrany folder";
        prefs.edit().putString("saf_uri", uri.toString()).putString("saf_label", label).apply();

        try { mobile.callAttr("set_saf_target", uri.toString(), label); } catch (Exception e) {
            Toast.makeText(this, "Błąd ustawiania folderu: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }

        SafSync.syncAll(this);
        if (webView != null) {
            webView.evaluateJavascript("refresh()", null);
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }
}
