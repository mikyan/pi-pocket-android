package app.pipocket.android;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import org.json.JSONObject;

/** Non-exported PendingIntent destination. Token-bearing stdout is never written to logs. */
public final class TermuxResultReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        SharedPreferences saved = RuntimeService.preferences(context);
        String nonce = intent.getData() == null ? "" : intent.getData().getLastPathSegment();
        String phase = intent.getData() == null ? "" : intent.getData().getHost();
        if (nonce == null || !nonce.equals(saved.getString("requestNonce", ""))) return;
        if (System.currentTimeMillis() - saved.getLong("requestAt", 0) > 20 * 60_000L) return;
        String failure = null;
        Bundle bundle = intent.getBundleExtra("result");
        try {
            if (bundle == null) throw new IllegalStateException("Termux 未返回执行结果。");
            JSONObject result = null;
            for (String line : bundle.getString("stdout", "").split("\\r?\\n")) {
                if (line.startsWith("POCKET_RESULT:")) {
                    if (result != null) throw new IllegalStateException("重复的组件状态。");
                    result = new JSONObject(line.substring("POCKET_RESULT:".length()));
                }
            }
            boolean success = bundle.getInt("exitCode", -1) == 0 && bundle.getInt("err", -1) == -1;
            if (success && "setup".equals(phase)) return; // An existing server is attached by the separate probe.
            if (!success || result == null || "error".equals(result.optString("status"))) {
                String detail = result == null ? bundle.getString("errmsg", "") : result.optString("message");
                String diagnostic = result == null ? bundle.getString("stderr", "") : result.optString("diagnostic", bundle.getString("stderr", ""));
                String heading = result == null
                    ? "Termux 未完成命令。请确认 allow-external-apps=true，且 Termux 可正常打开。"
                    : "Pi Pocket 在 Termux 中准备或启动失败，请查看下面的具体原因。";
                failure = heading + "\n" + detail + "\n" + diagnostic;
            } else if ("ping".equals(phase) && "accepted".equals(result.optString("status"))) {
                saved.edit().putString("acceptedNonce", nonce).commit();
                return;
            } else if ("probe".equals(phase)) {
                String state = result.getString("status");
                String token = result.getString("token");
                String version = result.getString("runtimeVersion");
                if (!(state.equals("ready") || state.equals("deferred")) || token.isEmpty() || token.length() > 1024
                        || token.matches(".*[\\x00-\\x20\\x7f].*") || !version.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,99}")) {
                    throw new IllegalStateException("无效的连接信息。");
                }
                saved.edit().putString("ownerToken", token).putString("runtimeVersion", version)
                    .putString("resultStatus", state).commit();
            } else {
                throw new IllegalStateException("未知的 Termux 回调。");
            }
        } catch (Exception e) {
            failure = "无法读取 Termux 执行结果：" + e.getMessage() + " 请检查 allow-external-apps 设置后重试。";
        }
        if (failure != null) {
            failure = RuntimeService.redact(failure, RuntimeService.ownerToken(context));
            if (failure.length() > 12000) failure = failure.substring(0, 12000);
            RuntimeService.appendLog(context, failure);
        }
        saved.edit().putString("resultError", failure == null ? "" : failure)
            .putString("resultNonce", nonce).putLong("resultAt", System.currentTimeMillis()).commit();
    }
}
