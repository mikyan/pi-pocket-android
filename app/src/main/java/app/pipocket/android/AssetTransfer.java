package app.pipocket.android;

import android.content.Context;
import android.content.res.AssetFileDescriptor;
import android.util.Base64;
import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/** One expiring loopback capability transfers one fixed asset, never a filesystem path. */
final class AssetTransfer implements Closeable {
    private final Context context;
    private final ServerSocket listener;
    private final String path;
    private final long length;
    private final ScheduledFuture<?> expiry;
    private volatile Socket activeSocket;
    private volatile boolean closed;
    private static final ScheduledExecutorService TIMER = Executors.newSingleThreadScheduledExecutor(task -> {
        Thread thread = new Thread(task, "pocket-transfer-expiry");
        thread.setDaemon(true);
        return thread;
    });

    AssetTransfer(Context context) throws Exception {
        this.context = context.getApplicationContext();
        try (AssetFileDescriptor descriptor = context.getAssets().openFd("pocket.tgz")) {
            length = descriptor.getLength();
        }
        byte[] random = new byte[32];
        new SecureRandom().nextBytes(random);
        path = "/" + Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING) + "/pocket.tgz";
        listener = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
        listener.setSoTimeout(1000);
        expiry = TIMER.schedule(this::close, 15, TimeUnit.MINUTES);
        Thread worker = new Thread(this::serve, "pocket-asset-transfer");
        worker.setDaemon(true);
        worker.start();
    }

    String url() { return "http://127.0.0.1:" + listener.getLocalPort() + path; }

    private void serve() {
        long deadline = System.currentTimeMillis() + 15 * 60_000L;
        try {
            while (!closed && System.currentTimeMillis() < deadline) {
                try (Socket socket = listener.accept()) {
                    activeSocket = socket;
                    socket.setSoTimeout(3000);
                    InputStream input = new BufferedInputStream(socket.getInputStream());
                    String request = readLine(input);
                    String host = null;
                    boolean valid = request.equals("GET " + path + " HTTP/1.1");
                    int total = request.length();
                    for (int count = 0; ; count++) {
                        String line = readLine(input);
                        total += line.length();
                        if (count > 64 || total > 8192) throw new IllegalStateException("Invalid HTTP headers");
                        if (line.isEmpty()) break;
                        int colon = line.indexOf(':');
                        if (colon <= 0) valid = false;
                        if (colon > 0 && line.substring(0, colon).equalsIgnoreCase("Host")) {
                            if (host != null) valid = false;
                            host = line.substring(colon + 1).trim();
                        }
                    }
                    valid &= ("127.0.0.1:" + listener.getLocalPort()).equals(host);
                    OutputStream output = socket.getOutputStream();
                    if (!valid) {
                        output.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
                        output.flush();
                        continue;
                    }
                    listener.close(); // Consume before sending; retries require a new capability.
                    output.write(("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                        + "Cache-Control: no-store\r\nConnection: close\r\nContent-Length: " + length + "\r\n\r\n")
                        .getBytes(StandardCharsets.US_ASCII));
                    try (InputStream asset = context.getAssets().open("pocket.tgz")) {
                        byte[] buffer = new byte[65536];
                        for (int count; (count = asset.read(buffer)) != -1;) output.write(buffer, 0, count);
                    }
                    output.flush();
                    break;
                } catch (SocketTimeoutException ignored) {
                    // Recheck the bounded lifetime.
                } catch (Exception ignored) {
                    if (listener.isClosed()) break;
                } finally {
                    activeSocket = null;
                }
            }
        } finally { close(); }
    }

    private static String readLine(InputStream input) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        for (int value; (value = input.read()) != -1;) {
            if (value == '\n') {
                byte[] bytes = output.toByteArray();
                if (bytes.length == 0 || bytes[bytes.length - 1] != '\r') throw new IllegalStateException("Invalid HTTP line");
                return new String(bytes, 0, bytes.length - 1, StandardCharsets.US_ASCII);
            }
            if (output.size() >= 4096) throw new IllegalStateException("HTTP line too long");
            output.write(value);
        }
        throw new IllegalStateException("Incomplete HTTP request");
    }

    @Override public void close() {
        closed = true;
        try { listener.close(); } catch (Exception ignored) { }
        Socket active = activeSocket;
        if (active != null) try { active.close(); } catch (Exception ignored) { }
        expiry.cancel(false);
    }
}
