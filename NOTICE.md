# 第三方组件与分发记录

本项目是 pi-pocket 的独立 Android 宿主，未修改锁定的上游源码，不是官方 Termux 发行版。`com.termux` 包名用于当前二进制运行前缀兼容，不表示官方背书。

## 已知输入与声明位置

| 组件 | 来源与版本记录 | 声明位置 |
| --- | --- | --- |
| pi-pocket | [TannerMidd/pi-pocket](https://github.com/TannerMidd/pi-pocket)，版本/提交见 `upstream.lock.json` | 上游 `LICENSE`，MIT，Copyright (c) 2026 Tanner Middleton；随源码保留 |
| pi durable 与 npm 生产依赖 | 固定上游 `package-lock.json` | 各 npm 包的 LICENSE/NOTICE/package.json；随实际生产依赖保留 |
| Termux bootstrap | `runtime.lock.json` 中固定发布 URL 与 SHA-256 | bootstrap 中 `share/doc`、`share/LICENSES` 及包记录 |
| Node.js、Git、bash、GNU 工具与其他 Android 包 | [Termux 包仓库](https://github.com/termux/termux-packages)，完整二进制版本与 SHA-256 见 `runtime.lock.json` | 各包自己的许可证与版权声明；不能统一标记为 MIT |
| Android Gradle 构建工具 | 根 `build.gradle` 与 Gradle wrapper 配置 | 各工具的上游许可 |

Termux app 仓库本身声明 [GPLv3-only 与组件例外](https://github.com/termux/termux-app/blob/master/LICENSE.md)。本项目宿主源码与运行包应按实际复用内容分别记录；仅引用 Termux 设计或包名不能替代逐组件许可判断。Node.js、OpenSSL、Git、bash、npm 等也各有自己的许可，最终以锁定版本携带的声明为准。

## 发布材料状态

当前工程记录了二进制来源、版本和 SHA-256，并保留包内及上游随附声明。它**尚未提供一份完整、逐组件归档的对应源码发行包**。这些记录不能单独替代适用许可证要求的源码及构建材料。

面向第三方分发 APK 前，应按本次实际二进制清单补齐并核对：对应版本源码、Termux 构建配方与补丁、必要的构建说明、版权和许可文本，以及适用许可证要求的源码提供方式。特别是包含 GPL/LGPL 等组件时，应核对最终分发形态，不能只附最新上游仓库链接便声称材料完整。

维护者更新 `runtime.lock.json` 或 `upstream.lock.json` 时，应同步更新这份记录和对应发布材料。不要删除 payload 内已有的版权、LICENSE 或 NOTICE 文件。
