# PlatformIO Offline Installer

这是一个独立子项目，用于生成和分发 PlatformIO 的离线安装器，并内置 SDK 管理入口。

目标：

- 在没有外网的 Windows 机器上安装 PlatformIO 基础环境
- 安装离线 VS Code 扩展
- 写入离线运行所需配置
- 提供 SDK 的导入、打包、清理管理入口
- 尽量降低用户操作复杂度

## 当前使用版本

- PlatformIO Core: `6.1.19`
- PlatformIO IDE VS Code Extension: `3.3.4` (`platformio-ide-3.3.4-offline.vsix`)

## 目录结构

- `source/launcher`
  安装器 GUI 源码与 PyInstaller spec

- `source/launcher/sdk_manager_window.py`
  统一的安装器 + SDK 管理窗口实现

- `resources/vscode`
  离线 VS Code 安装包

- `resources/vsix`
  PlatformIO 与 C/C++ 的离线 VSIX 扩展包

- `resources/runtime`
  内置 Python 运行时

- `resources/pio-data`
  预置 `.platformio` 数据

- `resources/dependencies/platformio-deps.yml`
  平台 / 工具链 / 框架依赖表，`level` 支持 `required` / `warning` / `optional` / `ignored`

- `resources/tools`
  SDK 管理所需的 `7z.exe` / `7z.dll`

- `resources/sdk`
  默认存放离线 SDK 包（包含上传器等公共依赖，如 `tool-esptoolpy`）

- `build_installer.ps1`
  构建目录版安装器的脚本

- `build`
  PyInstaller 中间产物

- `dist`
  最终发布产物

## 当前推荐构建方式

在本目录执行：

```powershell
.\build_installer.ps1
```

## 当前推荐发布入口

构建完成后，实际发给用户的是目录版安装器中的这个入口：

`dist/PlatformIO_Offline_Installer/PlatformIO_Offline_Installer.exe`

如果要直接给用户下载使用，当前推荐发布包是：

`dist/PlatformIO_Offline_Installer.zip`

用户下载后只需要解压，再运行目录里的：

`PlatformIO_Offline_Installer/PlatformIO_Offline_Installer.exe`

当前这个 ZIP 发布包已经内置：

- `espressif32-54.03.20.sdk.7z`
- `espressif32-7.0.0.sdk.7z`

因此用户不需要先自己打包 SDK，就可以直接安装 PIO 并导入这两个常用 SDK。

## 当前界面行为

- 程序启动后不再自动执行安装
- 主界面是一个统一窗口，使用标签页切换：
  - `安装 PIO`
  - `安装 SDK`
  - `打包 SDK`
  - `清理 SDK`
- 打包 SDK 时会锁定当前选择，避免打包过程中误改列表

## 当前推荐发布说明

- 暂时只发布 Windows `exe` 目录版
- GitHub Release 建议上传 `PlatformIO_Offline_Installer.zip`
- 不建议用户直接下载源码后自己构建
- 安装器内置 SDK 时，应优先把常用版本放入 `resources/sdk`

## 当前发布版本

- 当前整理好的首个正式发布说明：`docs/releases/v1.0.0.md`
- 推荐配套 tag：`v1.0.0`
