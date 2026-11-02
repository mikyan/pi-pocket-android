# 第三方组件

本项目是原版 pi-pocket 的独立 Android 宿主，包名 `io.github.mikyan.pipocket`，不是 Termux 官方发行版。0.3 系列通过公开的 RUN_COMMAND 接口调用用户另行安装的 Termux；APK 不再分发 Termux bootstrap、PRoot、Node、Git 或 GNU 系统工具二进制。

| APK 中的组件 | 来源和版本 | 声明 |
| --- | --- | --- |
| pi-pocket | [TannerMidd/pi-pocket](https://github.com/TannerMidd/pi-pocket)，见 `upstream.lock.json` | 上游 MIT LICENSE 原样随源码保留 |
| pi durable 与 npm 生产依赖 | 上游原始 `package-lock.json` | 保留各包的 LICENSE、NOTICE 与 package.json，不能统一视为 MIT |
| Android 构建工具 | Gradle wrapper 与构建脚本 | 使用各工具自己的许可证 |

Termux 及通过其包管理器安装的工具来自各自分发渠道，独立于本 APK 的资产；对应许可证由各上游提供。这里的来源说明不替代依赖自身的许可文本。

构建时不删除上游或 npm 依赖的版权声明，发布工件记录源提交、包锁和归档哈希。旧 0.2 系列内置二进制 APK 的分发材料与许可证边界仅适用于那些历史产物，不能以本次架构变更追溯替代。
