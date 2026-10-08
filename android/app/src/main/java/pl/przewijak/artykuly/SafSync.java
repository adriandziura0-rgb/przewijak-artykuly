package pl.przewijak.artykuly;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import androidx.documentfile.provider.DocumentFile;

import com.chaquo.python.Python;

import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;

public final class SafSync {
    private SafSync() {}

    public static boolean syncAll(Context context) {
        SharedPreferences prefs = context.getSharedPreferences("przewijak", Context.MODE_PRIVATE);
        String rawUri = prefs.getString("saf_uri", "");
        if (rawUri == null || rawUri.isEmpty()) return false;

        File root;
        try {
            String path = Python.getInstance().getModule("mobile_entry").callAttr("get_output_root").toString();
            root = new File(path);
        } catch (Exception e) {
            return false;
        }
        if (!root.isDirectory()) return false;

        DocumentFile tree = DocumentFile.fromTreeUri(context, Uri.parse(rawUri));
        if (tree == null || !tree.canWrite()) return false;

        syncDirectory(context, root, tree);
        return true;
    }

    private static void syncDirectory(Context context, File local, DocumentFile remote) {
        File[] children = local.listFiles();
        if (children == null) return;
        for (File child : children) {
            if (child.getName().startsWith(".tmp_") || child.getName().equals(".write_test")) continue;
            try {
                if (child.isDirectory()) {
                    DocumentFile dir = remote.findFile(child.getName());
                    if (dir == null || !dir.isDirectory()) {
                        if (dir != null) dir.delete();
                        dir = remote.createDirectory(child.getName());
                    }
                    if (dir != null) syncDirectory(context, child, dir);
                } else if (child.isFile()) {
                    DocumentFile out = remote.findFile(child.getName());
                    if (out == null || !out.isFile()) {
                        if (out != null) out.delete();
                        out = remote.createFile(mimeFor(child.getName()), child.getName());
                    }
                    if (out == null) continue;
                    try (FileInputStream in = new FileInputStream(child);
                         OutputStream os = context.getContentResolver().openOutputStream(out.getUri(), "wt")) {
                        if (os == null) continue;
                        byte[] buffer = new byte[65536];
                        int n;
                        while ((n = in.read(buffer)) > 0) os.write(buffer, 0, n);
                        os.flush();
                    }
                }
            } catch (Exception ignored) {
                // Błąd jednego pliku nie zatrzymuje synchronizacji pozostałych.
            }
        }
    }

    private static String mimeFor(String name) {
        String n = name.toLowerCase();
        if (n.endsWith(".html") || n.endsWith(".htm")) return "text/html";
        if (n.endsWith(".json")) return "application/json";
        if (n.endsWith(".csv")) return "text/csv";
        return "text/plain";
    }
}
