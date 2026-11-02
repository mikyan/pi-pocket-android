# Pi Pocket Android

把固定版本的原版 [pi-pocket](https://github.com/TannerMidd/pi-pocket) 和本地运行环境装进一个 Android APK。点击图标后，应用准备内置组件、启动 pi-pocket，再直接进入它的前端；使用者无需另装 Termux，也无需输入启动命令。

当前工程面向 **ARM64、Android 8.0 / API 26 及以上、设备主用户中的侧载安装**。Android 宿主使用原生 Java、前台 Service 和 WebView；运行环境包含 Termux bootstrap、Node.js、Git、shell 工具和上游生产依赖。首次展开和配置组件不要求现场下载这些依赖；调用在线模型、登录提供商及联网工具仍需要网络。

**尚未完成 Android 真机验证。** 构建成功或桌面集成测试通过，均不代表手机上的运行、任务恢复和上游全部功能已验证。语音、网页推送通知、分享接收、下载及浏览器自动化尚未完成 Android 适配；系统运行状态通知与网页推送是不同功能。验收范围见 [真机验证清单](docs/device-validation.md)。

## 安装兼容性

首版 `applicationId` 使用 **`com.termux`**，因为内置官方二进制使用 `/data/data/com.termux/files/usr` 固定前缀。它不能与已有 Termux 同装；不同签名的包也不能直接覆盖安装。

**已有 Termux 的设备请停止此次安装，保留原安装和数据。不要为试用本项目直接卸载 Termux。** 请选择没有安装 Termux 的备用 ARM64 设备测试。本版不支持工作资料、应用分身或次要 Android 用户；这些环境的私有目录不符合固定前缀。独立新包名需要重新构建相应运行环境，不能只改 Gradle 的 applicationId。

`targetSdk` 当前为 28，这是保留传统 Termux 二进制执行方式的侧载构建。它不是 Google Play 发行配置，也不承诺未来 Android 版本长期兼容。现代 targetSDK 的迁移路径见 [架构说明](docs/architecture.md)。

## 构建

需要以下本地工具：

- 完整 JDK 17 或 21，包含 `java`、`javac`、`jlink`；仅有 JRE 不够。
- Android SDK，安装 Android SDK Platform 36 和对应构建工具，并配置 `ANDROID_HOME` 或 `local.properties` 中的 `sdk.dir`。
- Python 3.12+、Git、Node.js 22.19+ 和 npm。
- 首次构建的网络连接与足够磁盘空间，用于下载 Gradle、SDK 组件、锁定的 Android 包和 npm 依赖。

在本项目根目录运行以下 PowerShell 命令：

```powershell
python scripts/prepare.py --source D:/Code/pi-pocket
.\gradlew.bat :app:assembleDebug
```

`--source` 指向可读取的本地 pi-pocket Git 仓库。构建脚本使用 `git archive` 提取锁定提交到 `.cache/upstream`，不会修改该仓库，也不会打包其现有 `node_modules` 或未提交改动。也可以省略 `--source`，由脚本拉取锁定的上游仓库。

工程已提供 `runtime.lock.json`，正常构建**不要加** `--update-runtime-lock`。只有维护者明确要生成第一份锁文件或更新 Android 依赖时，才运行：

```powershell
python scripts/prepare.py --source D:/Code/pi-pocket --update-runtime-lock
```

准备阶段会生成 `app/src/main/assets/runtime.zip` 和 `payload.json`；Gradle 在打包前核对 payload SHA-256，缺少内置运行环境时会拒绝构建。调试 APK 默认输出到 `app/build/outputs/apk/debug/app-debug.apk`。这是调试签名产物；后续升级安装必须保持签名一致并增加版本号。

## 上游与依赖锁定

| 输入 | 当前约束 |
| --- | --- |
| pi-pocket | `0.21.0`，提交 `7d6fbb0fc07adf18fa2d8e23514092176e5b65d2` |
| 上游来源 | `upstream.lock.json` |
| Android 包 | `runtime.lock.json` 固定版本、下载路径和每个文件的 SHA-256 |
| npm 依赖 | 上游原始 `package-lock.json`，干净目录执行 `npm ci` |
| 最终 payload | `payload.json` 中的 SHA-256，构建与设备安装分别校验 |

上游源码保持原样，不转译或重写服务端。当前生产安装使用 `--omit=dev --omit=optional --ignore-scripts`，宿主设置 `PI_POCKET_GUARD=off`；因此不将 Guard 或可选原生依赖功能列为已支持。用户的模型凭据不写入 APK。第三方来源、许可证与发布材料见 [NOTICE](NOTICE.md)。

## 数据与任务恢复

设备内持久数据位于 `/data/data/com.termux/files/home/`：

- `.pi-pocket/`：会话数据库、任务、配置、上传文件等。
- `.pi/agent/`：Pi 提供商登录和相关配置。
- `workspace/`：默认项目工作目录。

重新打开应用会连接仍存活的服务，或以相同数据目录启动原版 launcher。宿主不会把最后一条消息重新发送来模拟恢复。pi durable 会恢复其持久化运行状态；不能安全重放的中断工具可能显示 `interrupted`，任意 shell 或外部操作并不保证恰好执行一次。前台服务提高进程存活机会，系统仍可能终止它。

APK 升级替换 `usr` 运行环境，保留上述 `home` 数据。切换前会备份 `.pi-pocket` 与 `.pi/agent` 到应用私有 `files/backups/<时间戳>/`，并保留旧运行环境 `usr.previous-<时间戳>`。`workspace` 被保留，但不包含在这份自动备份中。备份同样在应用私有存储，卸载或清除应用数据会一并删除；它不等同于设备外备份。

上游 `0.21.0` 的存储迁移有方向性。保留旧运行环境不代表可以直接用旧版本打开新数据库，本项目不会自动回滚数据库。升级和失败恢复步骤见 [真机验证清单](docs/device-validation.md)。

## 验证与排查

已完成本地构建验证：真实上游的 4 组 smoke 检查、3 个 Python 打包单元测试，以及 Gradle `assembleDebug`、`lintDebug` 均通过。Lint 为 0 errors / 7 warnings，主要涉及固定运行前缀、WebView JavaScript、中文字符串和版本提示。payload 审计确认 248 个 `src/web/bin` 原始文件与锁定上游一致，已检查内置 deb 的 SHA-256 和 1,170 个符号链接的路径边界。新增运行包仍须随每次重打包重新完成校验。

以上是构建与桌面契约验证。尚未验证 Android 真机启动，也未实测手机上 durable 任务在中断后的续跑。

桌面契约测试使用独立临时数据目录，不访问手机或用户现有服务：

```powershell
$env:PI_POCKET_TEST_UPSTREAM = (Resolve-Path .cache/upstream).Path
node --test tests/upstream.integration.test.mjs
```

手机启动失败时，页面提供“查看启动日志”和重试入口。日志保存在 `home/pocket.log`，宿主会遮盖已识别的 owner token 等凭据。若提示端口或进程锁被占用，先等待已有任务；确需重启时，可在 Android 设置中完整停止此应用后重新打开。不要清除数据来处理启动问题。

详细运行流程见 [架构说明](docs/architecture.md)，需要完成的设备测试见 [真机验证清单](docs/device-validation.md)。
