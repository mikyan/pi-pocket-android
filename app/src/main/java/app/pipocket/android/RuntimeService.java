package app.pipocket.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

/** Termux owns the long-running command; host process death never stops user tasks. */
public final class RuntimeService extends Service {
    static final int PORT = 18787;
    static final String ORIGIN = "http://127.0.0.1:" + PORT;
    static final String RUN_COMMAND_PERMISSION = "com.termux.permission.RUN_COMMAND";
    static final String ACTION_RESTART = "app.pipocket.android.RESTART_TERMUX";
    static volatile String status = "正在连接 Termux…";
    static volatile String error;
    static volatile boolean ready;
    private volatile boolean starting;
    private volatile boolean destroyed;
    private volatile AssetTransfer transfer;
    private int latestStartId;
    private static final Object LOG_LOCK = new Object();

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("runtime", "Pi Pocket 连接状态", NotificationManager.IMPORTANCE_LOW));
        startForeground(1, notification("正在连接 Termux"));
    }

    @Override public synchronized int onStartCommand(Intent intent, int flags, int startId) {
        latestStartId = startId;
        startForeground(1, notification(status));
        if (!starting) {
            starting = true;
            error = null;
            boolean restart = intent != null && ACTION_RESTART.equals(intent.getAction());
            new Thread(() -> runRuntime(restart), "pocket-termux-connect").start();
        }
        return START_NOT_STICKY;
    }

    private void runRuntime(boolean restart) {
        try {
            ready = false;
            getPackageManager().getPackageInfo("com.termux", 0);
            if (checkSelfPermission(RUN_COMMAND_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
                throw new IllegalStateException("请授予 Pi Pocket 的 Termux 命令权限（RUN_COMMAND），然后重试。");
            }
            TermuxPayload payload = new TermuxPayload(this);
            String token = ownerToken(this);
            if (!restart && token != null && authenticatedReady(token)) {
                connected(payload.version);
                return;
            }
            String nonce = UUID.randomUUID().toString();
            preferences(this).edit().putString("requestNonce", nonce).putLong("requestAt", System.currentTimeMillis())
                .remove("resultNonce").remove("resultError").remove("acceptedNonce").commit();
            setStatus("正在确认 Termux 命令权限与外部应用设置…");
            dispatch(nonce, "ping", "printf '%s\\n' 'POCKET_RESULT:{\"status\":\"accepted\"}'\n");
            long deadline = System.currentTimeMillis() + 30_000L;
            while (!destroyed && !nonce.equals(preferences(this).getString("acceptedNonce", ""))) {
                checkFailure(nonce);
                if (System.currentTimeMillis() > deadline) throw new IllegalStateException("Termux 未接受命令。请打开 Termux 并启用 allow-external-apps=true，随后重试。");
                Thread.sleep(250);
            }
            if (destroyed) return;
            setStatus(restart ? "正在请求 Termux 停止并重启 Pi Pocket…" : "正在 Termux 中准备依赖并恢复会话；首次安装可能需要数分钟…");
            transfer = new AssetTransfer(this);
            String url = transfer.url();
            dispatch(nonce, "setup", payload.script(this, url, nonce, restart ? "restart" : "start"));
            dispatch(nonce, "probe", payload.script(this, "", nonce, "probe"));
            deadline = System.currentTimeMillis() + 19 * 60_000L;
            while (!destroyed && System.currentTimeMillis() < deadline) {
                checkFailure(nonce);
                if (nonce.equals(preferences(this).getString("resultNonce", ""))) {
                    token = ownerToken(this);
                    if (token != null && authenticatedReady(token)) {
                        connected(payload.version);
                        return;
                    }
                    throw new IllegalStateException("Termux 已返回连接信息，但 Pi Pocket 没有通过本地身份验证。请查看启动日志后重试。");
                }
                Thread.sleep(500);
            }
            if (!destroyed) throw new IllegalStateException("等待 Termux 超时。请确认网络可用后重试。诊断日志：~/.local/share/pi-pocket-android/logs/。");
        } catch (PackageManager.NameNotFoundException e) {
            error = "需要先安装并打开官方 Termux（com.termux），完成首次初始化。";
            setStatus("需要安装 Termux");
        } catch (Exception e) {
            error = redact(e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage(), ownerToken(this));
            appendLog(this, error);
            setStatus("Termux 连接需要处理");
        } finally {
            synchronized (this) {
                AssetTransfer active = transfer;
                if (active != null) active.close();
                transfer = null;
                stopForeground(true);
                stopSelfResult(latestStartId); // Never stop a newer request already queued by Android.
                starting = false; // Open the gate only after the old stop request has been issued.
            }
        }
    }

    private void checkFailure(String nonce) {
        SharedPreferences saved = preferences(this);
        if (nonce.equals(saved.getString("resultNonce", ""))) {
            String failure = saved.getString("resultError", "");
            if (!failure.isEmpty()) throw new IllegalStateException(failure);
        }
    }

    private void dispatch(String nonce, String phase, String script) {
        Intent callback = new Intent(this, TermuxResultReceiver.class).setData(Uri.parse("pipocket://" + phase + "/" + nonce));
        int pendingFlags = PendingIntent.FLAG_ONE_SHOT;
        if (Build.VERSION.SDK_INT >= 31) pendingFlags |= PendingIntent.FLAG_MUTABLE;
        PendingIntent pending = PendingIntent.getBroadcast(this, 0, callback, pendingFlags);
        Intent request = new Intent("com.termux.RUN_COMMAND")
            .setComponent(new ComponentName("com.termux", "com.termux.app.RunCommandService"))
            .putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash")
            .putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[] {"-s"})
            .putExtra("com.termux.RUN_COMMAND_STDIN", script)
            .putExtra("com.termux.RUN_COMMAND_WORKDIR", "/data/data/com.termux/files/home")
            .putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
            .putExtra("com.termux.RUN_COMMAND_PENDING_INTENT", pending)
            .putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "Pi Pocket " + phase)
            .putExtra("com.termux.RUN_COMMAND_COMMAND_DESCRIPTION", "在 Termux 中连接或启动 Pi Pocket；安装缺少的命令依赖。");
        if (startService(request) == null) throw new IllegalStateException("Termux 拒绝命令。请打开 Termux 并启用 allow-external-apps=true。");
    }

    private void connected(String wantedVersion) {
        ready = true;
        error = null;
        String actual = preferences(this).getString("runtimeVersion", "");
        setStatus(wantedVersion.equals(actual) ? "已连接 Termux 中的 Pi Pocket"
            : "已连接现有任务；新版组件等待你确认重启后安装");
        appendLog(this, status);
    }

    static SharedPreferences preferences(Context context) { return context.getSharedPreferences("termux-connection", Context.MODE_PRIVATE); }
    static String ownerToken(Context context) { return preferences(context).getString("ownerToken", null); }

    static boolean authenticatedReady(String token) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(ORIGIN + "/api/me").openConnection(Proxy.NO_PROXY);
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(1500);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Authorization", "Bearer " + token);
            return connection.getResponseCode() == 200;
        } catch (Exception ignored) { return false; }
        finally { if (connection != null) connection.disconnect(); }
    }

    static void appendLog(Context context, String text) {
        synchronized (LOG_LOCK) {
            try {
                File log = new File(context.getFilesDir(), "termux-host.log");
                if (log.length() > 128 * 1024) {
                    File previous = new File(context.getFilesDir(), "termux-host.log.1");
                    if (previous.exists()) previous.delete();
                    log.renameTo(previous);
                }
                try (FileOutputStream out = new FileOutputStream(log, true)) {
                    out.write((redact(text, ownerToken(context)) + "\n").getBytes(StandardCharsets.UTF_8));
                }
            } catch (Exception ignored) { }
        }
    }

    static String logs(Context context) {
        File log = new File(context.getFilesDir(), "termux-host.log");
        try (RandomAccessFile input = new RandomAccessFile(log, "r")) {
            byte[] bytes = new byte[(int) Math.min(16000, input.length())];
            input.seek(input.length() - bytes.length);
            input.readFully(bytes);
            return redact(new String(bytes, StandardCharsets.UTF_8), ownerToken(context));
        } catch (Exception ignored) {
            return "暂无宿主连接日志。Termux 组件日志：~/.local/share/pi-pocket-android/logs/setup.log 和 server.log。";
        }
    }

    static String redact(String text, String token) {
        if (text == null) return "";
        if (token != null && !token.isEmpty()) text = text.replace(token, "[已隐藏]");
        return text.replaceAll("http://127\\.0\\.0\\.1:[0-9]+/[^\\s\"']+", "[本地连接地址已隐藏]")
            .replaceAll("(?i)([?&](?:token|key|code)=)[^&\\s]+", "$1[已隐藏]")
            .replaceAll("(?i)(authorization[:=]\\s*(?:bearer\\s+)?)[^\\s]+", "$1[已隐藏]")
            .replaceAll("(?i)(\"(?:ownerToken|token|apiKey)\"\\s*:\\s*\")[^\"]+", "$1[已隐藏]");
    }

    private void setStatus(String text) {
        status = text;
        if (!destroyed) getSystemService(NotificationManager.class).notify(1, notification(text));
    }

    private Notification notification(String text) {
        PendingIntent pending = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "runtime").setContentTitle("Pi Pocket").setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_sync_noanim).setContentIntent(pending).setOngoing(true).build();
    }

    @Override public void onDestroy() {
        destroyed = true;
        AssetTransfer active = transfer;
        if (active != null) active.close();
        super.onDestroy(); // Never stop Termux or its server when this host dies.
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
