package app.pipocket.android;

import android.content.Context;
import android.os.Build;
import android.system.Os;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Iterator;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Installs a verified, versioned payload without touching persistent user data. */
final class RuntimeInstaller {
    interface Progress { void set(String value); }

    static File install(Context context, Progress progress) throws Exception {
        File files = context.getFilesDir();
        JSONObject payload = readJson(context.getAssets().open("payload.json"));
        if (payload.getInt("schemaVersion") != 1) throw new IllegalStateException("不支持的运行环境版本。");
        String architecture = payload.getString("architecture");
        if (!Arrays.asList(Build.SUPPORTED_ABIS).contains(architecture)) {
            throw new IllegalStateException("此安装包需要 " + architecture + " 安卓设备。");
        }
        String checksum = payload.getString("sha256");
        if (!checksum.matches("[a-fA-F0-9]{64}")) throw new IllegalStateException("运行环境校验信息无效。");
        File prefix = new File(files, "usr");
        recoverInterruptedSwitch(files, prefix);
        if (matches(prefix, checksum)) return prefix;

        progress.set("正在校验内置组件…");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buffer = new byte[65536];
        try (InputStream in = context.getAssets().open("runtime.zip")) {
            for (int n; (n = in.read(buffer)) != -1;) digest.update(buffer, 0, n);
        }
        StringBuilder actual = new StringBuilder();
        for (byte b : digest.digest()) actual.append(String.format("%02x", b & 255));
        if (!actual.toString().equalsIgnoreCase(checksum)) throw new IllegalStateException("内置组件校验失败，请重新安装完整 APK。");

        File stage = new File(files, "usr.staging-" + System.currentTimeMillis());
        mkdir(stage);
        List<String[]> links = new ArrayList<>();
        JSONObject permissions = null;
        progress.set("首次启动正在展开运行环境，请稍候…");
        try {
            try (ZipInputStream zip = new ZipInputStream(context.getAssets().open("runtime.zip"))) {
                for (ZipEntry entry; (entry = zip.getNextEntry()) != null;) {
                    String name = entry.getName();
                    if (name.equals("SYMLINKS.txt")) {
                        String text = new String(readAll(zip, 8 * 1024 * 1024), StandardCharsets.UTF_8);
                        for (String line : text.split("\n")) {
                            if (line.trim().isEmpty()) continue;
                            String[] parts = line.trim().split("←", -1);
                            if (parts.length != 2) throw new IllegalStateException("无效的符号链接清单。");
                            links.add(parts);
                        }
                    } else if (name.equals("permissions.json")) {
                        permissions = new JSONObject(new String(readAll(zip, 32 * 1024 * 1024), StandardCharsets.UTF_8));
                    } else {
                        File output = safeChild(stage, name);
                        if (entry.isDirectory()) mkdir(output);
                        else {
                            mkdir(output.getParentFile());
                            try (FileOutputStream out = new FileOutputStream(output)) {
                                for (int n; (n = zip.read(buffer)) != -1;) out.write(buffer, 0, n);
                            }
                            Os.chmod(output.getAbsolutePath(), 0600);
                        }
                    }
                    zip.closeEntry();
                }
            }
            if (permissions == null) throw new IllegalStateException("缺少运行环境文件权限清单。");
            for (Iterator<String> it = permissions.keys(); it.hasNext();) {
                String name = it.next();
                File target = safeChild(stage, name);
                if (!target.exists()) throw new IllegalStateException("权限清单引用了不存在的文件：" + name);
                Os.chmod(target.getAbsolutePath(), permissions.getInt(name) & 0777);
            }
            // Links are created last so extraction cannot follow an archive-supplied directory link.
            for (String[] link : links) {
                File output = safeChild(stage, link[1]);
                mkdir(output.getParentFile());
                String target = relocateLinkTarget(stage, output, link[0]);
                validateLinkTarget(stage, output, target);
                Os.symlink(target, output.getAbsolutePath());
            }
            if (!new File(stage, "bin/bash").isFile() || !new File(stage, "share/pi-pocket/bin/pi-pocket.js").isFile()
                    || !new File(stage, "share/pocket-host/setup-runtime.sh").isFile()
                    || !new File(stage, "bin/proot").isFile()
                    || !new File(stage, "libexec/proot/loader").isFile()) {
                throw new IllegalStateException("内置组件不完整，缺少引导程序或 pi-pocket。");
            }
            writeJson(new File(stage, ".pocket-payload.json"), payload);
            if (prefix.exists()) {
                progress.set("正在备份会话与提供商配置…");
                File backup = new File(files, "backups/" + System.currentTimeMillis());
                mkdir(backup);
                copyBackup(new File(files, "home/.pi-pocket"), new File(backup, "pi-pocket"));
                copyBackup(new File(files, "home/.pi/agent"), new File(backup, "pi-agent"));
                writeJson(new File(backup, "backup.json"), new JSONObject().put("reason", "before-runtime-upgrade")
                    .put("nextPayload", payload).put("automaticRestore", false));
            }
            File previous = new File(files, "usr.previous-" + System.currentTimeMillis());
            JSONObject transaction = new JSONObject().put("previous", previous.getName());
            File journal = new File(files, ".pocket-runtime-switch.json");
            writeJson(journal, transaction);
            if (prefix.exists() && !prefix.renameTo(previous)) throw new IllegalStateException("无法保存旧运行环境，请完整停止应用后重试。");
            if (!stage.renameTo(prefix)) {
                if (previous.exists() && !previous.renameTo(prefix)) throw new IllegalStateException("运行环境切换中断；旧环境已保存在 " + previous.getName());
                throw new IllegalStateException("运行环境切换失败，已保留旧环境。");
            }
            journal.delete();
            return prefix;
        } catch (Exception e) {
            if (stage.exists()) deleteStage(stage);
            throw e;
        }
    }

    static boolean isCurrent(Context context) {
        try {
            JSONObject wanted = readJson(context.getAssets().open("payload.json"));
            return matches(new File(context.getFilesDir(), "usr"), wanted.getString("sha256"));
        } catch (Exception ignored) { return false; }
    }

    private static boolean matches(File prefix, String checksum) {
        try (InputStream in = new FileInputStream(new File(prefix, ".pocket-payload.json"))) {
            return checksum.equalsIgnoreCase(readJson(in).getString("sha256"));
        } catch (Exception ignored) { return false; }
    }

    private static void recoverInterruptedSwitch(File files, File prefix) throws Exception {
        File journal = new File(files, ".pocket-runtime-switch.json");
        if (!journal.exists()) return;
        JSONObject pending = readJson(new FileInputStream(journal));
        File previous = safeChild(files, pending.getString("previous"));
        if (!prefix.exists() && previous.exists() && !previous.renameTo(prefix)) {
            throw new IllegalStateException("无法恢复上次保留的运行环境。");
        }
        journal.delete();
    }

    private static File safeChild(File root, String name) throws Exception {
        if (name.isEmpty() || name.startsWith("/") || name.contains("\\") || name.indexOf('\0') >= 0) throw new IllegalStateException("无效的运行环境路径。");
        File child = new File(root, name);
        String base = root.getCanonicalPath() + File.separator;
        if (!child.getCanonicalPath().startsWith(base)) throw new IllegalStateException("运行环境路径越界。");
        return child;
    }

    private static void validateLinkTarget(File stage, File link, String target) throws Exception {
        File destination;
        if (target.startsWith("/")) destination = new File(target);
        else {
            // Check lexical containment first; a previously installed absolute link
            // can legitimately resolve into the final usr prefix instead of staging.
            java.nio.file.Path lexical = link.getParentFile().toPath().resolve(target).normalize();
            if (!lexical.startsWith(stage.toPath().normalize())) throw new IllegalStateException("符号链接目标越界。");
            destination = lexical.toFile();
        }
        String resolved = destination.getCanonicalPath();
        String finalPrefix = new File(stage.getParentFile(), "usr").getCanonicalPath();
        if (!within(resolved, stage.getCanonicalPath()) && !within(resolved, finalPrefix)
                && !within(resolved, "/system") && !within(resolved, "/apex")) {
            throw new IllegalStateException("不支持的符号链接目标：" + target);
        }
    }

    private static String relocateLinkTarget(File stage, File link, String target) throws Exception {
        String guestPrefix = RuntimeService.GUEST_PREFIX;
        if (target.equals(guestPrefix) || target.startsWith(guestPrefix + "/")) {
            String relative = target.equals(guestPrefix) ? "" : target.substring(guestPrefix.length() + 1);
            java.nio.file.Path destination = stage.toPath().resolve(relative).normalize();
            if (!destination.startsWith(stage.toPath().normalize())) throw new IllegalStateException("符号链接目标越界。");
            // A relative target works before and after staging is renamed and
            // cannot accidentally resolve into the separately installed Termux.
            return link.getParentFile().toPath().relativize(destination).toString();
        }
        return target;
    }

    private static boolean within(String path, String root) { return path.equals(root) || path.startsWith(root + "/"); }

    private static void deleteStage(File file) throws Exception {
        if (!Files.isSymbolicLink(file.toPath()) && file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) for (File child : children) deleteStage(child);
        }
        if (!file.delete() && file.exists()) throw new IllegalStateException("无法清理未完成的组件目录。");
    }

    private static void copyBackup(File source, File destination) throws Exception {
        if (Files.isSymbolicLink(source.toPath())) {
            mkdir(destination.getParentFile());
            Files.createSymbolicLink(destination.toPath(), Files.readSymbolicLink(source.toPath()));
        } else if (source.isDirectory()) {
            mkdir(destination);
            File[] children = source.listFiles();
            if (children == null) throw new IllegalStateException("无法读取待备份目录。");
            for (File child : children) copyBackup(child, new File(destination, child.getName()));
        } else if (source.isFile()) {
            mkdir(destination.getParentFile());
            try (InputStream in = new FileInputStream(source); FileOutputStream out = new FileOutputStream(destination)) {
                byte[] buffer = new byte[65536];
                for (int n; (n = in.read(buffer)) != -1;) out.write(buffer, 0, n);
                out.getFD().sync();
            }
            Os.chmod(destination.getAbsolutePath(), 0600);
        }
    }

    static void mkdir(File file) throws Exception {
        if (!file.isDirectory() && !file.mkdirs()) throw new IllegalStateException("无法创建目录：" + file.getName());
    }

    static JSONObject readJson(InputStream in) throws Exception {
        try (InputStream stream = in) {
            return new JSONObject(new String(readAll(stream, 4 * 1024 * 1024), StandardCharsets.UTF_8));
        }
    }

    private static byte[] readAll(InputStream in, int limit) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        for (int n; (n = in.read(buffer)) != -1;) {
            if (bytes.size() + n > limit) throw new IllegalStateException("组件清单过大。");
            bytes.write(buffer, 0, n);
        }
        return bytes.toByteArray();
    }

    private static void writeJson(File file, JSONObject value) throws Exception {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(value.toString().getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        }
    }
}
