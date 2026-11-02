package app.pipocket.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
import android.widget.TextView;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Shows the unmodified upstream frontend once the private local server is ready. */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private WebView web;
    private LinearLayout startup;
    private TextView message;
    private Button retry;
    private ProgressBar spinner;
    private boolean checking;
    private boolean loaded;
    private boolean resumed;
    private boolean dead;
    private ValueCallback<Uri[]> upload;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(16, 21, 30));
        getWindow().setNavigationBarColor(Color.rgb(16, 21, 30));
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(16, 21, 30));
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(16, 21, 30));
        web.setVisibility(View.GONE);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        buildStartup(root);
        setContentView(root);
        configureWebView();
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] { Manifest.permission.POST_NOTIFICATIONS }, 42);
        }
        startRuntime();
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
        description.setText("你的本地 Agent 工作空间");
        description.setTextColor(Color.rgb(156, 170, 189));
        description.setTextSize(15);
        description.setGravity(Gravity.CENTER);
        description.setPadding(0, 10, 0, 34);
        startup.addView(description, new LinearLayout.LayoutParams(-1, -2));
        spinner = new ProgressBar(this);
        startup.addView(spinner, new LinearLayout.LayoutParams(64, 64));
        message = new TextView(this);
        message.setTextColor(Color.rgb(218, 225, 235));
        message.setGravity(Gravity.CENTER);
        message.setPadding(0, 28, 0, 20);
        startup.addView(message, new LinearLayout.LayoutParams(-1, -2));
        retry = new Button(this);
        retry.setText("重试");
        retry.setVisibility(View.GONE);
        retry.setOnClickListener(view -> { loaded = false; startRuntime(); tick.run(); });
        startup.addView(retry, new LinearLayout.LayoutParams(-2, -2));
        Button logs = new Button(this);
        logs.setText("查看启动日志");
        logs.setOnClickListener(view -> new AlertDialog.Builder(this).setTitle("启动日志（最近部分）")
            .setMessage(RuntimeService.logs(this)).setPositiveButton("关闭", null).show());
        startup.addView(logs, new LinearLayout.LayoutParams(-2, -2));
        root.addView(startup, new FrameLayout.LayoutParams(-1, -1));
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
                    startup.setVisibility(View.VISIBLE);
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
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == 8787;
    }

    private void startRuntime() {
        startForegroundService(new Intent(this, RuntimeService.class));
    }

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (dead || !resumed || loaded) return;
            String problem = RuntimeService.error;
            message.setText(problem == null ? RuntimeService.status : problem);
            retry.setVisibility(problem == null ? View.GONE : View.VISIBLE);
            spinner.setVisibility(problem == null ? View.VISIBLE : View.GONE);
            if (!checking) {
                checking = true;
                network.execute(() -> {
                    String token = RuntimeService.ownerToken(MainActivity.this);
                    boolean available = token != null && RuntimeService.authenticatedReady(token);
                    handler.post(() -> {
                        checking = false;
                        if (dead || !resumed || loaded || !available) return;
                        loaded = true;
                        web.setVisibility(View.VISIBLE);
                        startup.setVisibility(View.GONE);
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
                    startup.setVisibility(View.VISIBLE);
                    web.setVisibility(View.GONE);
                    message.setText("正在重新连接本地服务…");
                    startRuntime();
                    handler.removeCallbacks(tick);
                    handler.post(tick);
                });
            });
        } else handler.post(tick);
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
