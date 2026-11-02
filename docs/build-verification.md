# 本次构建记录

日期：2026-11-02。产物：`pi-pocket-android-0.1.0-arm64-debug.apk`。

已完成：

- pi-pocket `0.21.0`，固定提交 `7d6fbb0fc07adf18fa2d8e23514092176e5b65d2`；原本地仓库工作区保持干净。
- 原版 launcher 的桌面契约测试通过：启动、owner 鉴权、登录 Cookie、前端资源、官方重启接口及重启后身份保持。未发送模型请求。
- Python 打包边界测试 3 项通过。
- 离线负载审计通过：19,698 个归档文件，59 个 Android 包 SHA-256 匹配，248 个上游核心源码/前端/入口文件逐字节保持原样，1,170 条符号链接路径通过检查。
- Gradle `:app:assembleDebug :app:lintDebug` 成功。Lint 为 0 错误、7 警告，涉及固定 Termux 路径、必要 JavaScript、中文常量及构建工具版本提示。
- APK v2 签名校验成功；包名 `com.termux`、min SDK 26、target SDK 28、版本 `0.1.0`。

APK SHA-256：

```text
2f59ea6905f1a23cc63a595b8f8f5b6a1f9dfe65aae07a785a80c71a19cca717
```

尚未验证：没有连接 Android 设备，所以尚未在 ARM64 设备执行首次解包/dpkg 配置、WebView、模型登录、工具调用、杀进程后 durable 恢复、应用升级或文件上传。安装脚本内的设备自检代码已经打包，但不能算作已运行通过。该 APK 是待真机验收的调试版本。

桌面测试使用现有 Node `v24.14.1`；APK 内锁定的 Android Node 为 `24.22.2`。构建没有修改电脑全局 Node，也没有替换用户现有 Termux 或 pi-pocket。
