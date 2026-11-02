# 设备验证范围

0.3 系列的自动设备检查由 `scripts/device-smoke.py` 执行，只允许在 Android 模拟器上运行，避免把清理或 force-stop 测试误用于个人手机。具体成功记录以对应 GitHub Actions 运行及报告为准。

## 2026-11-03 验收记录（香港时间）

[Actions #10](https://github.com/mikyan/pi-pocket-android/actions/runs/41488961177) 全流程通过，产物为 [0.3.0-ci.10 / Android 1010](https://github.com/mikyan/pi-pocket-android/releases/tag/android-1010-pi-0.27.0)。测试源码提交为 `ed387714b66f70f31367efa3e0e674f3fddacec5`，上游为 pi-pocket 0.27.0。

设备为 Android 15 / API 35 x86_64 模拟器，安装官方 Termux 0.118.3，从尚未安装 Node 的环境开始。实际设备脚本用时 130.9 秒，验证了权限拒绝、禁用外部命令、依赖部署、认证接口、前端渲染、仅停止壳应用后的重连，以及强停 Termux 后仅打开 Pi Pocket 的服务恢复。恢复前后 owner 保持一致，预置 Termux 文件完整；三次页面检查均检测到 18 个可见标签及 17 个可操作控件，最终截图为恢复后的原版 Sessions 页面。

发布后重新下载校验了全部附件，APK 大小为 35,367,750 字节，归档内容与 payload 哈希相符，没有内置原生运行库。APK v2 签名通过，证书 SHA-256 与 Android 1003 版本一致：`55a839545f86b4c99c1f42c9c09166d02ef85a362848e3f9c531c5df6ca82538`。

以上不包含 ARM64 真机、实际模型请求或进行中 agent 任务恢复测试。

## 自动检查与待验收范围

自动检查覆盖：

- 安装官方 Termux 并完成初始化，保留一个预置私有测试文件。
- 安装独立包名 Pi Pocket，验证 RUN_COMMAND 权限缺失时不正常进入前端，随后配置授权。
- 通过真实 Termux 接口安装所需工具、导入固定源码，启动原版服务。
- owner 鉴权、原版页面内容和 WebView 实际渲染。
- 停止壳应用后，Termux 服务仍存活；重新打开使用同一 owner。
- 停止 Termux 后重新打开，保留同一数据并恢复服务。
- 预置 Termux 私有文件不被覆盖或删除。

尚需分别验收：

- 各品牌 ARM64 真机、后台省电策略、长时间休眠后重连。
- 模型登录、真实工具调用、进行中 durable 任务被中断后的恢复及重复副作用检查。
- 前后版本数据库升级、升级中断、旧版在运行时安装新 APK 后延迟应用更新。
- 手机文件上传、共享存储授权、浏览器 OAuth 回跳。

自动检查不发送模型请求，不把页面能打开当作以上功能全部通过。旧 0.1/0.2 架构的成功记录只属于当时实现。
