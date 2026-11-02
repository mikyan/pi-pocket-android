package app.pipocket.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Shows the unmodified upstream frontend once the private local server is ready. */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private WebView web;
    private LinearLayout startup;
    private ScrollView startupScroll;
    private TextView message;
    private Button retry;
    private ProgressBar spinner;
    private TextView introduction;
    private LinearLayout toolbar;
    private boolean checking;
    private boolean loaded;
    private boolean resumed;
    private boolean dead;
    private boolean awaitingPermission;
    private ValueCallback<Uri[]> upload;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(16, 21, 30));
        getWindow().setNavigationBarColor(Color.rgb(16, 21, 30));
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(12), 0, dp(6), 0);
        toolbar.setBackgroundColor(Color.rgb(16, 21, 30));
        toolbar.setVisibility(View.GONE);
        TextView caption = new TextView(this);
        caption.setText("Pi Pocket");
        caption.setTextColor(Color.WHITE);
        caption.setTextSize(15);
        toolbar.addView(caption, new LinearLayout.LayoutParams(0, -2, 1));
        Button settings = new Button(this);
        settings.setAllCaps(false);
        settings.setText("Termux");
        settings.setTextSize(12);
        settings.setOnClickListener(view -> showRuntimeMenu());
        toolbar.addView(settings, new LinearLayout.LayoutParams(-2, dp(48)));
        layout.addView(toolbar, new LinearLayout.LayoutParams(-1, dp(48)));
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(16, 21, 30));
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(16, 21, 30));
        web.setVisibility(View.GONE);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        buildStartup(root);
        layout.addView(root, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(layout);
        configureWebView();
        if (getPreferences(MODE_PRIVATE).getBoolean("termuxAccepted", false)) startRuntime();
        else showSetup();
    }

    private void buildStartup(FrameLayout root) {
        startup = new LinearLayout(this);
        startup.setOrientation(LinearLayout.VERTICAL);
        startup.setGravity(Gravity.CENTER);
        int padding = (int) (28 * getResources().getDisplayMetrics().density);
        startup.setPadding(padding, padding, padding, padding);
        TextView title = new TextView(this);
        title.setText("Pi Pocket");
        title.setTextSize(30);
        title.setTextColor(Color.WHITE);
        title.setGravity(Gravity.CENTER);
        startup.addView(title, new LinearLayout.LayoutParams(-1, -2));
        TextView description = new TextView(this);
        description.setText("在 Termux 中运行你的本地 Agent");
        description.setTextColor(Color.rgb(156, 170, 189));
        description.setTextSize(15);
        description.setGravity(Gravity.CENTER);
        description.setPadding(0, 10, 0, 34);
        startup.addView(description, new LinearLayout.LayoutParams(-1, -2));
        introduction = new TextView(this);
        introduction.setText("连接已安装的 Termux，自动准备并启动原版 pi-pocket。首次安装缺少的工具需要联网。会话与项目保存在 Termux 中。");
        introduction.setTextColor(Color.rgb(156, 170, 189));
        introduction.setTextSize(14);
        introduction.setGravity(Gravity.CENTER);
        introduction.setPadding(0, 0, 0, dp(12));
        startup.addView(introduction, new LinearLayout.LayoutParams(-1, -2));
        spinner = new ProgressBar(this);
        startup.addView(spinner, new LinearLayout.LayoutParams(64, 64));
        message = new TextView(this);
        message.setTextColor(Color.rgb(218, 225, 235));
        message.setGravity(Gravity.CENTER);
        message.setPadding(0, 28, 0, 20);
        startup.addView(message, new LinearLayout.LayoutParams(-1, -2));
        retry = new Button(this);
        retry.setAllCaps(false);
        retry.setText("重试");
        retry.setVisibility(View.GONE);
        retry.setOnClickListener(view -> connectFromUser());
        startup.addView(retry, new LinearLayout.LayoutParams(-2, -2));
        Button help = new Button(this);
        help.setAllCaps(false);
        help.setText("Termux 配置帮助");
        help.setOnClickListener(view -> showHelp());
        startup.addView(help, new LinearLayout.LayoutParams(-2, -2));
        Button logs = new Button(this);
        logs.setText("查看启动日志");
        logs.setOnClickListener(view -> showLogs());
        startup.addView(logs, new LinearLayout.LayoutParams(-2, -2));
        startupScroll = new ScrollView(this);
        startupScroll.setFillViewport(true);
        startupScroll.addView(startup, new ScrollView.LayoutParams(-1, -1));
        root.addView(startupScroll, new FrameLayout.LayoutParams(-1, -1));
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private String termuxProblem() {
        try {
            ServiceInfo service = getPackageManager().getServiceInfo(
                new ComponentName("com.termux", "com.termux.app.RunCommandService"), 0);
            if (!service.enabled || !service.exported) return "Termux 的命令接口未启用。请安装提供 RUN_COMMAND 接口的官方版本。";
        } catch (PackageManager.NameNotFoundException e) {
            return "请先安装并打开一次官方 Termux。当前没有检测到它的命令接口，部分商店版本不提供此接口。";
        }
        if (checkSelfPermission(RuntimeService.RUN_COMMAND_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            return "需要授权 Pi Pocket 在 Termux 中执行命令。点击下方按钮完成授权。";
        }
        return null;
    }

    private void showSetup() {
        spinner.setVisibility(View.GONE);
        retry.setVisibility(View.VISIBLE);
        retry.setText("连接 Termux 并启动");
        String problem = termuxProblem();
        message.setText(problem == null ? "准备好后，点击下方按钮开始。" : problem);
    }

    private void connectFromUser() {
        loaded = false;
        getPreferences(MODE_PRIVATE).edit().putBoolean("termuxAccepted", true).apply();
        try {
            getPackageManager().getServiceInfo(new ComponentName("com.termux", "com.termux.app.RunCommandService"), 0);
        } catch (PackageManager.NameNotFoundException e) { showSetup(); showHelp(); return; }
        if (checkSelfPermission(RuntimeService.RUN_COMMAND_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            awaitingPermission = true;
            requestPermissions(new String[] { RuntimeService.RUN_COMMAND_PERMISSION }, 43);
            return;
        }
        startRuntime();
        handler.removeCallbacks(tick);
        handler.post(tick);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] { Manifest.permission.POST_NOTIFICATIONS }, 42);
        }
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request != 43) return;
        awaitingPermission = false;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) connectFromUser();
        else {
            showSetup();
            if (!shouldShowRequestPermissionRationale(RuntimeService.RUN_COMMAND_PERMISSION)) {
                new AlertDialog.Builder(this).setTitle("需要授权 Termux 命令权限")
                    .setMessage("系统已拒绝再次弹出授权窗口。请打开 Pi Pocket 的应用设置，在权限或附加权限中允许在 Termux 中执行命令，然后返回重试。")
                    .setNegativeButton("稍后", null)
                    .setPositiveButton("应用设置", (dialog, which) -> startActivity(new Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())))).show();
            }
        }
    }

    private void showHelp() {
        new AlertDialog.Builder(this).setTitle("连接 Termux")
            .setMessage("1. 安装官方 Termux 并打开一次，等待初始化完成。\n\n2. 在 Termux 中启用 allow-external-apps=true（可复制下方配置命令）。\n\n3. 返回这里，点击“连接 Termux 并启动”并允许执行命令。\n\n授权后，pi-pocket 可以使用 Termux 的文件、工具和已有 Pi 配置。手机共享存储仍取决于 Termux 自身的授权。")
            .setPositiveButton("打开 Termux", (dialog, which) -> openTermux())
            .setNeutralButton("复制配置命令", (dialog, which) -> {
                String command = "mkdir -p ~/.termux && { sed -i '/^[[:space:]]*allow-external-apps[[:space:]]*=/d' ~/.termux/termux.properties 2>/dev/null; printf '\\nallow-external-apps=true\\n' >> ~/.termux/termux.properties; } && termux-reload-settings";
                ClipboardManager clipboard = getSystemService(ClipboardManager.class);
                clipboard.setPrimaryClip(ClipData.newPlainText("Termux 配置", command));
                openTermux();
            }).setNegativeButton("安装说明", (dialog, which) ->
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://github.com/termux/termux-app#installation"))))
            .show();
    }

    private void openTermux() {
        Intent intent = getPackageManager().getLaunchIntentForPackage("com.termux");
        if (intent != null) startActivity(intent);
        else startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://github.com/termux/termux-app#installation")));
    }

    private void showLogs() {
        new AlertDialog.Builder(this).setTitle("启动日志（最近部分）")
            .setMessage(RuntimeService.logs(this)).setPositiveButton("关闭", null).show();
    }

    private void showRuntimeMenu() {
        new AlertDialog.Builder(this).setTitle("Termux 运行环境")
            .setItems(new String[] { "运行状态", "查看启动日志", "打开 Termux", "重启并应用 APK 中的版本", "配置帮助" }, (dialog, which) -> {
                if (which == 0) new AlertDialog.Builder(this).setTitle("运行状态")
                    .setMessage(RuntimeService.status + "\n\n会话和项目保存在 Termux 的 ~/.local/share/pi-pocket-android/。关闭此界面不会停止后台任务。")
                    .setPositiveButton("关闭", null).show();
                else if (which == 1) showLogs();
                else if (which == 2) openTermux();
                else if (which == 3) new AlertDialog.Builder(this).setTitle("重启 pi-pocket？")
                    .setMessage("这会中断当前 pi-pocket 进程，并使用 APK 中的版本重新打开同一份数据。请先等待重要任务完成。其他 Termux 会话不会被主动停止。")
                    .setNegativeButton("取消", null).setPositiveButton("重启", (d, w) -> {
                        loaded = false;
                        web.setVisibility(View.GONE);
                        toolbar.setVisibility(View.GONE);
                        startupScroll.setVisibility(View.VISIBLE);
                        startForegroundService(new Intent(this, RuntimeService.class).setAction(RuntimeService.ACTION_RESTART));
                        handler.removeCallbacks(tick);
                        handler.postDelayed(tick, 1000);
                    }).show();
                else showHelp();
            }).show();
    }

    private void configureWebView() {
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setSupportMultipleWindows(false);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (local(uri)) return false;
                if (request.isForMainFrame() && ("https".equals(uri.getScheme()) || "http".equals(uri.getScheme()) || "mailto".equals(uri.getScheme()))) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
                    catch (Exception ignored) { }
                }
                return true;
            }

            @Override public void onPageFinished(WebView view, String url) {
                if (url.startsWith(RuntimeService.ORIGIN + "/") && !url.contains("/login?")) CookieManager.getInstance().flush();
            }

            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError failure) {
                if (request.isForMainFrame()) {
                    loaded = false;
                    startupScroll.setVisibility(View.VISIBLE);
                    toolbar.setVisibility(View.GONE);
                    web.setVisibility(View.GONE);
                    message.setText("正在重新连接本地服务…");
                    startRuntime();
                    handler.removeCallbacks(tick);
                    handler.post(tick);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (upload != null) upload.onReceiveValue(null);
                upload = callback;
                Intent choose = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
                String[] accept = params.getAcceptTypes();
                if (accept != null && accept.length > 0 && !accept[0].isEmpty()) choose.putExtra(Intent.EXTRA_MIME_TYPES, accept);
                choose.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try { startActivityForResult(choose, 7); }
                catch (Exception e) { upload.onReceiveValue(null); upload = null; }
                return true;
            }
        });
    }

    static boolean local(Uri uri) {
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == RuntimeService.PORT;
    }

    private void startRuntime() {
        if (awaitingPermission) return;
        if (!getPreferences(MODE_PRIVATE).getBoolean("termuxAccepted", false) || termuxProblem() != null) {
            showSetup();
            return;
        }
        introduction.setVisibility(View.GONE);
        spinner.setVisibility(View.VISIBLE);
        retry.setVisibility(View.GONE);
        startForegroundService(new Intent(this, RuntimeService.class));
    }

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (dead || !resumed || loaded) return;
            if (!getPreferences(MODE_PRIVATE).getBoolean("termuxAccepted", false) || termuxProblem() != null) {
                showSetup();
                handler.postDelayed(this, 1000);
                return;
            }
            String problem = RuntimeService.error;
            message.setText(problem == null ? RuntimeService.status : problem);
            retry.setVisibility(problem == null ? View.GONE : View.VISIBLE);
            retry.setText("重试连接");
            spinner.setVisibility(problem == null ? View.VISIBLE : View.GONE);
            if (!checking) {
                checking = true;
                network.execute(() -> {
                    String token = RuntimeService.ownerToken(MainActivity.this);
                    boolean available = RuntimeService.ready && token != null && RuntimeService.authenticatedReady(token);
                    handler.post(() -> {
                        checking = false;
                        if (dead || !resumed || loaded || !available) return;
                        loaded = true;
                        web.setVisibility(View.VISIBLE);
                        toolbar.setVisibility(View.VISIBLE);
                        startupScroll.setVisibility(View.GONE);
                        web.loadUrl(RuntimeService.ORIGIN + "/login?token=" + Uri.encode(token));
                    });
                });
            }
            handler.postDelayed(this, 1000);
        }
    };

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        web.onResume();
        handler.removeCallbacks(tick);
        if (loaded) {
            // A WebView can remain alive while Android has stopped its native server.
            // Preserve the current page when healthy; reconnect only when needed.
            network.execute(() -> {
                String token = RuntimeService.ownerToken(MainActivity.this);
                boolean available = token != null && RuntimeService.authenticatedReady(token);
                handler.post(() -> {
                    if (dead || !resumed || available) return;
                    loaded = false;
                    startupScroll.setVisibility(View.VISIBLE);
                    toolbar.setVisibility(View.GONE);
                    web.setVisibility(View.GONE);
                    message.setText("正在重新连接本地服务…");
                    startRuntime();
                    handler.removeCallbacks(tick);
                    handler.post(tick);
                });
            });
        } else { startRuntime(); handler.post(tick); }
    }

    @Override protected void onPause() {
        resumed = false;
        handler.removeCallbacks(tick);
        web.onPause();
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != 7 || upload == null) return;
        Uri[] files = null;
        if (result == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                files = new Uri[clip.getItemCount()];
                for (int i = 0; i < files.length; i++) files[i] = clip.getItemAt(i).getUri();
            } else if (data.getData() != null) files = new Uri[] { data.getData() };
        }
        upload.onReceiveValue(files);
        upload = null;
    }

    @Override public void onBackPressed() {
        if (loaded && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);
    }

    @Override protected void onDestroy() {
        dead = true;
        handler.removeCallbacks(tick);
        network.shutdownNow();
        if (upload != null) upload.onReceiveValue(null);
        web.destroy();
        super.onDestroy();
    }
}
