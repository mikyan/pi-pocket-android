# Pi Pocket Android

把固定版本的原版 [pi-pocket](https://github.com/TannerMidd/pi-pocket) 和本地运行环境装进一个 Android APK。点击图标后，应用准备内置组件、启动 pi-pocket，再直接进入它的前端；使用者无需另装 Termux，也无需输入启动命令。

当前工程面向 **ARM64、Android 8.0 / API 26 及以上的侧载安装**。Android 宿主使用原生 Java、前台 Service 和 WebView；运行环境包含 PRoot、Termux bootstrap、Node.js、Git、shell 工具和上游生产依赖。首次展开和配置组件不要求现场下载这些依赖；调用在线模型、登录提供商及联网工具仍需要网络。另提供 x86_64 构建用于 CI 模拟器验证。

**尚未完成 Android 真机验证。** 构建成功或桌面集成测试通过，均不代表手机上的运行、任务恢复和上游全部功能已验证。语音、网页推送通知、分享接收、下载及浏览器自动化尚未完成 Android 适配；系统运行状态通知与网页推送是不同功能。验收范围见 [真机验证清单](docs/device-validation.md)。

## 安装兼容性

当前 `applicationId` 为 **`io.github.mikyan.pipocket`**，与官方 Termux 的 `com.termux` 是两个独立应用，可以同时安装。保留现有 Termux，无需卸载、覆盖或迁移它的数据。

内置二进制仍使用原编译前缀，但 PRoot 只在本应用的子进程中将虚拟 `/data/data/com.termux/files` 映射到本应用自己的私有目录。实际数据位于 `io.github.mikyan.pipocket` 的目录，不读取或修改另一应用的 Termux 目录；不需要 root 或外部 Termux 服务。监听端口为 `18787`，避开 pi-pocket 默认的 `8787`。PRoot 带来系统调用开销及设备兼容性边界，模拟器结果不能替代 ARM64 真机验收。

历史 `0.1.0` 的同包名原型已停用，不应安装到用户手机；当前构建采用 `0.2.0` 系列。

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

## GitHub Actions 构建与自动升级

[Build Android APK](https://github.com/mikyan/pi-pocket-android/actions/workflows/build-android.yml) 会在推送到 `main`、面向 `main` 的 Pull Request、手动触发，以及每周一香港时间 **09:17**（UTC 01:17）运行。每周检查上游正式 Release，跳过草稿和预发布版本，为新版本生成候选 `upstream.lock.json`。同一次运行的模拟器与 ARM64 构建使用同一份候选锁，并分别验证其 SHA-256。工作流使用 Ubuntu 24.04、Python 3.12、Node.js 24、JDK 17 和 Android SDK 36，不需要本地的 `D:/Code/pi-pocket`。

手动运行时，打开 Actions → Build Android APK → Run workflow，选择 `main`，可设置：

- `check_upstream`：默认开启，检查上游正式 Release；关闭时按仓库现有锁构建。
- `publish_release`：默认开启，所有检查通过后发布 GitHub Release；关闭时只生成 CI 工件，不推进仓库中的上游锁或发布 Release。

普通推送和 Pull Request 按仓库现有锁测试。每周检查若版本没有变化且已有匹配的正式 Release，就跳过模拟器和 APK 构建；如果缺少匹配 Release，会重新构建并补全发布。定时运行可能排队延迟；公开仓库连续 60 天没有活动时，GitHub 可能自动停用定时工作流，需要在 Actions 中重新启用。见 [GitHub 定时工作流说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

工作流先构建一次仅用于测试的 x86_64 APK，在 Android API 35 模拟器内安装官方 Termux，再安装独立包名的 Pi Pocket。设备检查包含两包同时存在、内置运行环境配置、认证接口与原版前端 HTML、WebView 实际渲染可操作控件、强行停止后重开并保留同一 owner，以及官方 Termux 私有测试文件未被更改。测试不发送模型请求，不等同于 ARM64 真机或运行中 Agent 任务恢复验收。模拟器检查失败时，后续 ARM64 APK 构建与发布会被阻止。

设备检查通过后，再构建 ARM64 包并运行打包单元测试、离线负载和上游源码一致性检查、原版 launcher 的启动/登录/重启测试、APK 编译、Android lint 和 APK 签名校验。只有这些步骤全部通过，发布任务才有写入权限，核对产物后推进上游锁，并将上传完整的草稿 Release 转为正式 [GitHub Release](https://github.com/mikyan/pi-pocket-android/releases)。测试或构建失败会保留旧锁和旧版 Release；发布中断留下的草稿不会作为正式版本提供下载，后续运行可以补全发布。Android 运行环境锁不会随上游升级自动更新，若新版本要求超出现有运行环境的 Node.js 版本，检查会失败，需要单独升级并验证运行环境。

`main` 成功构建后，运行页面的 **Artifacts** 中会出现 `pi-pocket-android-arm64-debug-<run_id>-<run_attempt>`，内含 APK、`SHA256SUMS.txt`、上游与运行环境锁、payload 元数据和构建记录。发布成功后也可直接从公开的 Releases 页面下载 APK 和校验材料。APK 工件保留 30 天，lint 和设备检查报告保留 14 天。GitHub Actions 工件下载需要登录 GitHub；正式 Release 下载不依赖工件保留期。Pull Request 和非 `main` 分支的手动构建只做检查，不发布 APK；x86_64 测试 APK 也不会发布。

仓库使用独立、稳定的 CI 调试签名，GitHub repository secrets 为 `ANDROID_DEBUG_KEYSTORE_BASE64` 和 `ANDROID_DEBUG_KEYSTORE_PASSWORD`，alias 为 `pocket-debug`。主分支构建缺少签名 secrets 时会失败，不会悄悄发布随机签名的包。密钥不进入 Git、构建缓存或下载工件，PR 和模拟器检查也不使用它。CI 版本号为 `0.2.0-ci.<运行编号>`，versionCode 为 `1000 + 运行编号`，后续应保持同一密钥以支持覆盖升级。

**此前在本机生成的测试 APK 使用本机 debug 签名，与 CI 专用签名不同。** 它们不能直接互相覆盖。已安装旧本地包时应先保留数据并安排迁移，不要为了换用 CI 包直接卸载应用。后续测试统一使用 CI 工件。

Actions 自身按完整提交 SHA 固定版本；仅缓存校验过的 runtime 下载与 Gradle 依赖下载，不缓存上游工作目录或私钥。工作流定义见 [build-android.yml](.github/workflows/build-android.yml)。

## 上游与依赖锁定

| 输入 | 当前约束 |
| --- | --- |
| pi-pocket | 当前版本与提交以 [upstream.lock.json](upstream.lock.json) 的 `version`、`commit` 为准 |
| 上游来源 | [upstream.lock.json](upstream.lock.json) 中的 `repository` |
| Android 包 | `runtime.lock.json` 固定版本、下载路径和每个文件的 SHA-256 |
| npm 依赖 | 上游原始 `package-lock.json`，干净目录执行 `npm ci` |
| 最终 payload | `payload.json` 中的 SHA-256，构建与设备安装分别校验 |

上游源码保持原样，不转译或重写服务端。当前生产安装使用 `--omit=dev --omit=optional --ignore-scripts`，宿主设置 `PI_POCKET_GUARD=off`；因此不将 Guard 或可选原生依赖功能列为已支持。用户的模型凭据不写入 APK。第三方来源、许可证与发布材料见 [NOTICE](NOTICE.md)。

## 数据与任务恢复

设备内持久数据位于本应用的 `files/home/`（主用户通常为 `/data/user/0/io.github.mikyan.pipocket/files/home/`）：

- `.pi-pocket/`：会话数据库、任务、配置、上传文件等。
- `.pi/agent/`：Pi 提供商登录和相关配置。
- `workspace/`：默认项目工作目录。

重新打开应用会连接仍存活的服务，或以相同数据目录启动原版 launcher。宿主不会把最后一条消息重新发送来模拟恢复。pi durable 会恢复其持久化运行状态；不能安全重放的中断工具可能显示 `interrupted`，任意 shell 或外部操作并不保证恰好执行一次。前台服务提高进程存活机会，系统仍可能终止它。

APK 升级替换 `usr` 运行环境，保留上述 `home` 数据。切换前会备份 `.pi-pocket` 与 `.pi/agent` 到应用私有 `files/backups/<时间戳>/`，并保留旧运行环境 `usr.previous-<时间戳>`。`workspace` 被保留，但不包含在这份自动备份中。备份同样在应用私有存储，卸载或清除应用数据会一并删除；它不等同于设备外备份。

上游升级可能进行单向存储迁移。保留旧运行环境不代表可以直接用旧版本打开新数据库，本项目不会自动回滚数据库。升级和失败恢复步骤见 [真机验证清单](docs/device-validation.md)。

## 验证与排查

每次实际构建都会检查打包边界、原版上游启动/登录/重启接口、源文件逐字节一致性、离线包 SHA-256、APK 签名和 Android lint。x86_64 模拟器先安装官方 Termux 与本应用，验证共存、服务启动、前端实际渲染和强行停止后重开；只有设备测试成功才构建并发布 ARM64 APK 工件。具体结果以对应 Actions 运行记录为准。

尚未验证 ARM64 真机启动，也未实测手机上 durable 模型任务在中断后的续跑。模拟器测试不会调用付费模型。

桌面契约测试使用独立临时数据目录，不访问手机或用户现有服务：

```powershell
$env:PI_POCKET_TEST_UPSTREAM = (Resolve-Path .cache/upstream).Path
node --test tests/upstream.integration.test.mjs
```

手机启动失败时，页面提供“查看启动日志”和重试入口。日志保存在 `home/pocket.log`，宿主会遮盖已识别的 owner token 等凭据。若提示端口或进程锁被占用，先等待已有任务；确需重启时，可在 Android 设置中完整停止此应用后重新打开。不要清除数据来处理启动问题。

详细运行流程见 [架构说明](docs/architecture.md)，需要完成的设备测试见 [真机验证清单](docs/device-validation.md)。
