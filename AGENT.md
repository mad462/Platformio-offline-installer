# AGENT Notes

## 项目定位

这个目录是一个独立项目：

- `platformio-offline-installer`

它的职责只有一个：

- 生成和分发 PlatformIO 的离线安装器

当前安装器已经内置了 SDK 管理标签页，相关实现保留在本仓库内：

- `source/launcher/sdk_manager_window.py`

## 目录归属原则

如果某个资源属于离线安装器，就必须放在本项目目录下面。

正确位置示例：

- `resources/pio-data`
- `resources/runtime`
- `resources/vscode`
- `resources/vsix`

不允许再把这些内容散落到工作区根目录，例如：

- `../pio-data`
- `../runtime`
- `../build`
- `../dist`

工作区根目录只应作为两个独立项目的容器存在。

## 当前关键资源

- `resources/pio-data`
  预置的 `.platformio` 离线环境数据

- `resources/runtime`
  安装器运行所需的内置 Python 运行时

- `resources/vscode`
  离线 VS Code 安装包

- `resources/vsix`
  PlatformIO / C-C++ 的离线 VSIX 扩展包

- `resources/patch`
  用于修补本地 PlatformIO VS Code 扩展的补丁文件

- `resources/tools`
  SDK 管理所需的 `7z.exe` / `7z.dll`

- `resources/sdk`
  默认存放离线 SDK 包

## 当前构建入口

构建脚本：

- `build_installer.ps1`

GUI 安装器源码：

- `source/launcher/gui_installer.py`

PyInstaller spec：

- `source/launcher/gui_installer.spec`

SDK 管理窗口源码：

- `source/launcher/sdk_manager_window.py`

## 当前推荐发布入口

打包后实际发给用户的是目录版安装器中的这个文件：

- `dist/PlatformIO_Offline_Installer/PlatformIO_Offline_Installer.exe`

不要把外层单文件残留 `exe` 当成正式发布入口。

## 当前已知版本

- PlatformIO Core: `6.1.19`
- PlatformIO IDE VS Code Extension: `3.3.4`

## 当前已知问题/历史经验

1. VS Code 用户设置里容易残留旧的 `platformio-ide.customPATH`
   正确目标应该是用户目录下的：
   - `.platformio/penv/Scripts`
   - `.platformio/portable-bin`

2. 不要再让安装器写入工作区根目录里的旧 `pio-data` 路径
   应始终写入用户目录 `.platformio`

3. 如果要上传到 GitHub，这个项目已经是独立仓库形态
   目录名就是：
   - `platformio-offline-installer`
