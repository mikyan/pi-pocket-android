package app.pipocket.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.IBinder;
import android.os.Build;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/** Owns the local launcher; user data stays outside the replaceable runtime. */
public final class RuntimeService extends Service {
    static final int PORT = 18787;
    static final String ORIGIN = "http://127.0.0.1:" + PORT;
    static final String GUEST_FILES = "/data/data/com.termux/files";
    static final String GUEST_PREFIX = GUEST_FILES + "/usr";
    static final String GUEST_HOME = GUEST_FILES + "/home";
    static volatile String status = "正在启动 Pi Pocket…";
    static volatile String error;
    static volatile boolean ready;
    private volatile boolean starting;
    private volatile boolean destroyed;
    private volatile Process launcher;
    private final Object logLock = new Object();

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("runtime", "Pi Pocket 运行状态", NotificationManager.IMPORTANCE_LOW));
        startForeground(1, notification("正在准备本地运行环境"));
    }

    @Override public synchronized int onStartCommand(Intent intent, int flags, int startId) {
        if (!starting && (launcher == null || !launcher.isAlive())) {
            starting = true;
            error = null;
            new Thread(this::runRuntime, "pocket-runtime").start();
        }
        return START_STICKY;
    }

    private void runRuntime() {
        try {
            ready = false;
            String token = ownerToken(this);
            if (token != null && authenticatedReady(token)) {
                ready = true;
                setStatus(RuntimeInstaller.isCurrent(this) ? "Pi Pocket 正在运行" : "已连接现有任务；完整停止应用后可安装新组件");
                return;
            }
            if (portInUse()) throw new IllegalStateException("本地端口 " + PORT + " 已被占用或服务尚未就绪。请稍候重试；若持续出现，请在系统设置中完整停止此应用后重新打开。");
            verifyRuntimeUnlocked();
            File prefix = RuntimeInstaller.install(this, this::setStatus);
            File home = new File(getFilesDir(), "home");
            File workspace = new File(home, "workspace");
            RuntimeInstaller.mkdir(workspace);
            RuntimeInstaller.mkdir(new File(prefix, "tmp"));
            File configured = new File(prefix, "share/pocket-host/configured");
            if (!configured.isFile()) {
                setStatus("正在配置内置 Node.js、Git 和命令行工具…");
                Process configuration = process(prefix, home, GUEST_PREFIX + "/bin/bash",
                    GUEST_PREFIX + "/share/pocket-host/setup-runtime.sh").start();
                drain(configuration);
                int result = configuration.waitFor();
                if (result != 0 || !configured.isFile()) throw new IllegalStateException("运行环境配置失败（退出码 " + result + "）。可查看日志后重试。");
            }
            if (destroyed) return;
            setStatus("正在恢复本地会话…");
            launcher = process(prefix, home,
                GUEST_PREFIX + "/bin/bash",
                GUEST_PREFIX + "/share/pocket-host/launch-pocket.sh",
                "--access", "local", "--host", "127.0.0.1", "--port", Integer.toString(PORT),
                "--data", GUEST_HOME + "/.pi-pocket",
                "--cwd", GUEST_HOME + "/workspace", "-y").start();
            new Thread(() -> {
                try { drain(launcher); }
                catch (Exception e) { appendLog("[host] 输出读取中断：" + e.getClass().getSimpleName()); }
            }, "pocket-output").start();
            long deadline = System.currentTimeMillis() + 180_000;
            while (!destroyed && launcher.isAlive() && System.currentTimeMillis() < deadline) {
                token = ownerToken(this);
                if (token != null && authenticatedReady(token)) {
                    ready = true;
                    setStatus("Pi Pocket 正在运行");
                    break;
                }
                Thread.sleep(500);
            }
            if (!ready && !destroyed) throw new IllegalStateException("Pi Pocket 未能完成启动。请查看日志；提供商登录可在进入应用后完成。");
            if (destroyed) return;
            int result = launcher.waitFor();
            ready = false;
            if (!destroyed) throw new IllegalStateException("本地运行进程已退出（" + result + "）。重新打开或点击重试可恢复已保存会话。");
        } catch (Exception e) {
            ready = false;
            error = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
            appendLog("[host] " + error);
            setStatus("启动需要处理");
        } finally {
            starting = false;
        }
    }

    private ProcessBuilder process(File prefix, File home, String... arguments) {
        // Termux paths exist only in this child process's translated view. The
        // actual files belong to our independent Android package and UID.
        List<String> command = new ArrayList<>(Arrays.asList(
            new File(prefix, "bin/proot").getAbsolutePath(),
            "--kill-on-exit", "-L", "-r", "/",
            "-b", getApplicationInfo().dataDir + ":/data/data/com.termux!",
            "-b", getFilesDir().getAbsolutePath() + ":" + GUEST_FILES + "!",
            "-w", GUEST_HOME,
            GUEST_PREFIX + "/bin/env", "-i",
            "HOME=" + GUEST_HOME,
            "PREFIX=" + GUEST_PREFIX,
            "PATH=" + GUEST_PREFIX + "/bin:/system/bin:/system/xbin",
            "TMPDIR=" + GUEST_PREFIX + "/tmp",
            "LANG=en_US.UTF-8", "TERM=xterm-256color",
            "SHELL=" + GUEST_PREFIX + "/bin/bash",
            "LD_LIBRARY_PATH=" + GUEST_PREFIX + "/lib",
            "SSL_CERT_FILE=" + GUEST_PREFIX + "/etc/tls/cert.pem",
            "GIT_SSL_CAINFO=" + GUEST_PREFIX + "/etc/tls/cert.pem",
            "TERMUX__PREFIX=" + GUEST_PREFIX,
            "TERMUX__HOME=" + GUEST_HOME,
            "TERMUX_APP__PACKAGE_NAME=" + getPackageName(),
            "TERMUX__APP_PACKAGE_NAME=" + getPackageName(),
            "ANDROID__BUILD_VERSION_SDK=" + Build.VERSION.SDK_INT,
            "ANDROID_ROOT=/system", "ANDROID_DATA=/data",
            "PI_POCKET_GUARD=off", "NO_COLOR=1"));
        command.addAll(Arrays.asList(arguments));
        ProcessBuilder builder = new ProcessBuilder(command);
        builder.directory(home).redirectErrorStream(true);
        Map<String, String> env = builder.environment();
        env.remove("LD_PRELOAD");
        env.put("LD_LIBRARY_PATH", new File(prefix, "lib").getAbsolutePath());
        env.put("PROOT_LOADER", new File(prefix, "libexec/proot/loader").getAbsolutePath());
        File loader32 = new File(prefix, "libexec/proot/loader32");
        if (loader32.isFile()) env.put("PROOT_LOADER_32", loader32.getAbsolutePath());
        else env.remove("PROOT_LOADER_32");
        env.put("PROOT_TMP_DIR", new File(prefix, "tmp").getAbsolutePath());
        env.put("PROOT_NO_SECCOMP", "1");
        return builder;
    }

    private void verifyRuntimeUnlocked() throws Exception {
        // This is a preflight check, not a lock held over the installation transaction.
        // Android serializes starts of this non-exported service; launch-pocket.sh holds
        // the lasting lock in the upstream launcher for the entire server lifetime.
        File prefix = new File(getFilesDir(), "usr");
        File home = new File(getFilesDir(), "home");
        File lock = new File(home, ".pi-pocket/android-host.lock");
        File flock = new File(prefix, "bin/flock");
        if (!lock.exists() || !flock.isFile()) return;
        Process probe = process(prefix, home, GUEST_PREFIX + "/bin/flock", "--exclusive", "--nonblock",
            GUEST_HOME + "/.pi-pocket/android-host.lock", GUEST_PREFIX + "/bin/true").start();
        if (!probe.waitFor(5, TimeUnit.SECONDS)) {
            probe.destroy();
            throw new IllegalStateException("无法确认后台进程状态。请在系统设置中完整停止此应用后重试。");
        }
        if (probe.exitValue() != 0) throw new IllegalStateException("已有任务进程占用运行环境。请稍后重试；升级组件前需在系统设置中完整停止此应用，或重启手机。");
    }

    private void drain(Process process) throws Exception {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            for (String line; (line = reader.readLine()) != null;) appendLog(line);
        }
    }

    private void appendLog(String text) {
        synchronized (logLock) {
            try {
                File home = new File(getFilesDir(), "home");
                RuntimeInstaller.mkdir(home);
                File log = new File(home, "pocket.log");
                if (log.length() > 2 * 1024 * 1024) {
                    File previous = new File(home, "pocket.log.1");
                    if (previous.exists()) previous.delete();
                    log.renameTo(previous);
                }
                String token = ownerToken(this);
                String clean = redact(text, token);
                try (FileOutputStream out = new FileOutputStream(log, true)) {
                    out.write((clean + "\n").getBytes(StandardCharsets.UTF_8));
                }
            } catch (Exception ignored) { }
        }
    }

    static String logs(Context context) {
        File log = new File(context.getFilesDir(), "home/pocket.log");
        try (RandomAccessFile in = new RandomAccessFile(log, "r")) {
            long length = in.length();
            byte[] bytes = new byte[(int) Math.min(16000, length)];
            in.seek(length - bytes.length);
            in.readFully(bytes);
            return redact(new String(bytes, StandardCharsets.UTF_8), ownerToken(context));
        } catch (Exception ignored) { return "暂无启动日志。"; }
    }

    private static String redact(String text, String token) {
        if (token != null && !token.isEmpty()) text = text.replace(token, "[已隐藏]");
        return text.replaceAll("(?i)([?&](?:token|key|code)=)[^&\\s]+", "$1[已隐藏]")
            .replaceAll("(?i)(authorization[:=]\\s*(?:bearer\\s+)?)[^\\s]+", "$1[已隐藏]");
    }

    static String ownerToken(Context context) {
        try {
            File config = new File(context.getFilesDir(), "home/.pi-pocket/config.json");
            String value = RuntimeInstaller.readJson(new FileInputStream(config)).getString("ownerToken");
            return value.isEmpty() ? null : value;
        } catch (Exception ignored) { return null; }
    }

    static boolean authenticatedReady(String token) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(ORIGIN + "/api/me").openConnection();
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(1500);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Authorization", "Bearer " + token);
            return connection.getResponseCode() == 200;
        } catch (Exception ignored) { return false; }
        finally { if (connection != null) connection.disconnect(); }
    }

    private boolean portInUse() {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", PORT), 500);
            return true;
        } catch (Exception ignored) { return false; }
    }

    private void setStatus(String text) {
        status = text;
        getSystemService(NotificationManager.class).notify(1, notification(text));
    }

    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "runtime").setContentTitle("Pi Pocket")
            .setContentText(text).setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
            .setContentIntent(pending).setOngoing(true).build();
    }

    @Override public void onDestroy() {
        destroyed = true;
        ready = false;
        // Android may restart this service; a surviving child is detected before any new spawn.
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
