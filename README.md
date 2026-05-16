# PlatformIO Offline Installer

这是一个独立子项目，用于生成和分发 PlatformIO 的离线安装器。

目标：

- 在没有外网的 Windows 机器上安装 PlatformIO 基础环境
- 安装离线 VS Code 扩展
- 写入离线运行所需配置
- 尽量降低用户操作复杂度

## 当前使用版本

- PlatformIO Core: `6.1.19`
- PlatformIO IDE VS Code Extension: `3.3.4` (`platformio-ide-3.3.4-offline.vsix`)

## 目录结构

- `source/launcher`
  安装器 GUI 源码与 PyInstaller spec

- `resources/vscode`
  离线 VS Code 安装包

- `resources/vsix`
  PlatformIO 与 C/C++ 的离线 VSIX 扩展包

- `resources/runtime`
  内置 Python 运行时

- `resources/pio-data`
  预置 `.platformio` 数据

- `build_installer.ps1`
  构建目录版安装器的脚本

## 当前推荐构建方式

在本目录执行：

```powershell
.\build_installer.ps1
```

## 当前推荐发布入口

构建完成后，实际发给用户的是目录版安装器中的这个入口：

`dist/PlatformIO_Offline_Installer/PlatformIO_Offline_Installer.exe`

## 说明

这个项目只负责“PlatformIO 离线安装器”本身。

SDK 的导入、打包、清理等逻辑属于另一个独立子项目：

- `../offline-sdk-manager`
