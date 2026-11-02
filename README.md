# Pi Pocket Android

[pi-pocket](https://github.com/TannerMidd/pi-pocket) 的 Android 入口。应用通过已安装的 **Termux** 启动原版 pi-pocket，并在 WebView 中直接显示它的前端。服务端、前端和 durable 任务逻辑保持上游原样；本仓库只负责 Android 集成、版本打包与发布。

从 **0.3.0** 开始全面依赖外部 Termux，不再内置 PRoot、Linux 根文件系统或 Termux 二进制。APK 包含锁定版本的 pi-pocket 源码及生产 JavaScript 依赖；Node.js、Git 和命令行工具使用 Termux 的安装环境。APK 不区分 CPU 架构，当前设备测试覆盖范围见 Actions 报告。

## 手机上使用

1. 安装支持 `RUN_COMMAND` 接口的 [官方 Termux](https://github.com/termux/termux-app#installation)，打开一次并等待初始化。部分商店版本没有此接口；不要为了换版本而直接卸载有重要数据的 Termux。
2. 在 Termux 的 `~/.termux/termux.properties` 中启用 `allow-external-apps=true`，运行 `termux-reload-settings`。应用中的“Termux 配置帮助”提供可复制命令。
3. 从 [Releases](https://github.com/mikyan/pi-pocket-android/releases) 安装 Pi Pocket APK。点击“连接 Termux 并启动”，授予在 Termux 中运行命令的权限。
4. 首次启动自动补齐缺少的工具，需要联网访问 Termux 包仓库。随后导入 APK 中的 pi-pocket，并打开前端。模型登录、API Key 等在原版前端中配置。

小米 HyperOS 找不到系统里的权限入口时，可直接使用应用内的授权按钮。必须同时满足 Android 的 `RUN_COMMAND` 授权和 Termux 的 `allow-external-apps` 设置。缺少权限、接口、依赖或端口冲突时，启动页提供提示、重试和日志。

右上角 **Termux** 菜单可查看状态、日志，打开 Termux，或明确重启 pi-pocket 以应用 APK 中的版本。关闭 Android 界面不会主动停止 Termux 中的任务。

## 数据与访问范围

| Termux 中的路径 | 内容 |
| --- | --- |
| `~/.local/share/pi-pocket-android/data/` | pi-pocket 会话、数据库、配置、上传文件 |
| `~/.local/share/pi-pocket-android/workspace/` | 默认项目工作目录 |
| `~/.local/share/pi-pocket-android/versions/` | 按版本安装的上游代码与依赖 |
| `~/.local/share/pi-pocket-android/logs/` | 运行日志 |
| `~/.local/share/pi-pocket-android/backups/` | 停止旧服务后、切换版本前的数据归档 |
| `~/.pi/agent/` | Pi 提供商配置和登录凭据，与 Termux 内已有 Pi 共用 |

工作目录不是文件沙箱。授权后的工具具有 Termux 用户的文件权限，能访问 Termux HOME 及 Termux 本身已获准访问的共享存储；不能读取其他 Android 应用的私有目录。Pi Pocket 不申请全盘存储权限。Android 文件选择器用于用户选择的上传文件。

应用只监听 `127.0.0.1:18787`，通过上游 owner token 登录。Android 宿主不通过 JavaScript bridge 暴露 shell。Termux 和 Pi Pocket 仍是两个独立 Android 包，分别为 `com.termux` 与 `io.github.mikyan.pipocket`。

更新或卸载 Pi Pocket 壳应用不会主动删除 Termux 中的数据；卸载 Termux 或清除 Termux 数据会删除其中的环境和会话。旧 0.2.x 内置模式的数据不会自动迁移，也不会在更新时删除；需要时请先自行导出。

## 恢复与升级

重新打开应用会先连接仍在运行的 pi-pocket，避免启动重复进程。Termux 进程被系统终止后，应用重新启动原版 launcher 并打开同一份持久数据。任务恢复由上游 pi durable 负责，宿主不会重发最后一条消息。数据库与会话保留不等同于所有外部工具操作都能无损续跑，任意 shell 命令也不保证恰好执行一次。

安装新版 APK 不会自动打断正在运行的旧服务。可继续使用当前任务，待合适时通过 Termux 菜单选择“重启并应用 APK 中的版本”。切换版本前会在服务停止、持有专用锁时备份 `data/`；备份失败则中止切换。备份不包含工作目录或共享的 Pi 凭据，仍保存在 Termux 内。升级可能包含上游数据库迁移；不要直接用旧版本打开已迁移的数据。

## 本地构建

需要 JDK 17、Android SDK 36、Python 3.12+、Git、Node.js 24 和 npm。在仓库根目录运行：

```powershell
python scripts/prepare.py
python scripts/verify-payload.py
.\gradlew.bat :app:assembleDebug :app:lintDebug
```

也可给 prepare 传 `--source D:/Code/pi-pocket`，从本地 Git 仓库提取锁定提交；不会修改该仓库或打包未提交改动。省略时拉取上游。产物为 `app/build/outputs/apk/debug/app-debug.apk`。

`upstream.lock.json` 固定上游提交、版本及最低 Node 要求。prepare 在干净目录执行 `npm ci --omit=dev --omit=optional --ignore-scripts`，生成 `assets/pocket.tgz` 和 `payload.json`。源码保持逐字节一致，npm 命令链接按包元数据重建为 POSIX 符号链接。构建和 Termux 导入均验证归档 SHA-256。Termux 系统包由手机上的包管理器维护，`termux-requirements.json` 记录集成要求，不声称锁定用户的整个 Termux 环境。

Pi TUI 包中随附的六个桌面原生 helper 不适用于 Android，构建时仅排除这些已明确识别的文件，并在 payload 元数据记录路径和哈希；JavaScript 源码及 QuickJS WebAssembly 保留。运行版本标识同时绑定上游归档与宿主脚本，单独修复启动脚本也会触发正确的版本切换。

## GitHub Actions 与 Release

[Build Android APK](https://github.com/mikyan/pi-pocket-android/actions/workflows/build-android.yml) 在 main 推送、Pull Request、手动触发及每周一香港时间 **09:17**（UTC 01:17）运行。每周检查 pi-pocket 的稳定 Release，发现新版本后选择固定提交，测试通过才更新仓库的上游锁并发布 APK；失败保留已有正式版本。

同一次运行的模拟器测试与正式构建使用同一份候选锁，并核对 SHA-256。设备测试安装真实官方 Termux，通过 `RUN_COMMAND` 启动服务，验证认证接口、WebView 渲染、壳应用停止后的重连，以及 Termux 停止后的恢复。测试不发送模型请求。之后执行原版 launcher 契约测试、打包审计、Gradle 编译、lint 和签名检查。发布步骤单独获得写权限，先上传完整草稿，再转为正式 Release。

每周没有新版本且已有对应 Release 时跳过构建。手动运行可设置 `check_upstream` 和 `publish_release`。公开仓库长期没有活动时，GitHub 可能停用定时工作流，维护者需检查 Actions 状态。

Release 提供通用 APK、SHA256SUMS、上游锁、Termux 要求、payload 元数据和构建记录。CI 使用稳定签名，版本为 `0.3.0-ci.<运行编号>`，versionCode 为 `1000 + 运行编号`。签名 secrets 为 `ANDROID_DEBUG_KEYSTORE_BASE64`、`ANDROID_DEBUG_KEYSTORE_PASSWORD`，alias 为 `pocket-debug`；PR 和模拟器测试不使用这些密钥。

本地默认 debug 签名与 CI 签名不同，不能直接互相覆盖。不要为了安装不同签名的包而直接删除重要数据。

## 支持边界

Android 8.0 / API 26 起的侧载应用，目前保留 targetSdk 28。Termux 本身也需要支持设备和 Android 版本。已安装的 Node 必须支持上游最低版本和 `node:sqlite`；过旧环境需按错误提示升级。

旧 Termux 初始环境可能带有不兼容新版 Node 的共享库。首次安装 Node 时会同步更新它的六个直接支持库；已安装的 Node 仅在明确出现 Android 链接器错误时尝试定向修复这些库。范围记录在 `termux-requirements.json`，不执行全局 `pkg upgrade`。

当前使用 `PI_POCKET_GUARD=off`，不包含 optional npm 包或生命周期脚本。语音、网页推送、Android 分享接收、下载及浏览器自动化尚未完整适配。实际通过的设备、版本和检查以对应 Actions 及 [设备验收记录](docs/device-validation.md) 为准，不把模拟器通过等同于所有手机或进行中模型任务恢复通过。

更多说明：[运行架构](docs/architecture.md) · [第三方组件](NOTICE.md)。
