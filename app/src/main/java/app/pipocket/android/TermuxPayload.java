package app.pipocket.android;

import android.content.Context;
import android.util.Base64;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/** Reads portable JavaScript assets; the installed Termux app supplies native executables. */
final class TermuxPayload {
    final JSONObject manifest;
    final String version;

    TermuxPayload(Context context) throws Exception {
        manifest = new JSONObject(readAsset(context, "payload.json", 65536));
        if (manifest.getInt("schemaVersion") != 2 || !"termux".equals(manifest.getString("runtime"))
                || !"pocket.tgz".equals(manifest.getString("archive"))) {
            throw new IllegalStateException("需要 Termux 模式的完整 Pi Pocket 安装包。");
        }
        version = manifest.getString("runtimeVersion");
        if (!version.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
                || !manifest.getString("sha256").matches("[a-f0-9]{64}")
                || !manifest.getString("piPocketCommit").matches("[a-f0-9]{40}")
                || !manifest.getString("minimumNode").matches("[0-9]+\\.[0-9]+\\.[0-9]+")) {
            throw new IllegalStateException("Pi Pocket 组件元数据无效。");
        }
    }

    String script(Context context, String url, String nonce, String operation) throws Exception {
        String launch = readAsset(context, "launch-pocket.sh", 65536);
        String setup = readAsset(context, "setup-runtime.sh", 131072);
        return "POCKET_DOWNLOAD_URL=" + quote(url) + "\n"
            + "POCKET_ARCHIVE_SHA256=" + quote(manifest.getString("sha256")) + "\n"
            + "POCKET_RUNTIME_VERSION=" + quote(version) + "\n"
            + "POCKET_MINIMUM_NODE=" + quote(manifest.getString("minimumNode")) + "\n"
            + "POCKET_REQUEST_NONCE=" + quote(nonce) + "\n"
            + "POCKET_OPERATION=" + quote(operation) + "\n"
            + "POCKET_LAUNCH_BASE64=" + quote(Base64.encodeToString(launch.getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP)) + "\n"
            + "POCKET_MANIFEST_BASE64=" + quote(Base64.encodeToString(manifest.toString().getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP)) + "\n"
            + setup;
    }

    static String quote(String value) { return "'" + value.replace("'", "'\\''") + "'"; }

    private static String readAsset(Context context, String name, int limit) throws Exception {
        try (InputStream input = context.getAssets().open(name); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            for (int length; (length = input.read(buffer)) != -1;) {
                if (output.size() + length > limit) throw new IllegalStateException("组件脚本过大。");
                output.write(buffer, 0, length);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
