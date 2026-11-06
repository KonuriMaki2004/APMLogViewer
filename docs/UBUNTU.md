# APMLogViewer 在 Ubuntu 上构建与安装（0.1.2）

面向 **Ubuntu 24.04 LTS x86_64**（在 `ubuntu` 机器上实测：Python 3.12.3 / 12 线程 / 30 GB）。
`.deb` 内置 PyInstaller onedir 产物（自带 Python + Qt 运行库），**不依赖系统 Python 包**。

## 1. 依赖

运行期依赖由包管理自动安装（`DEBIAN/control` 的 `Depends`）：

| 依赖 | 用途 |
| --- | --- |
| `libc6 (>= 2.35)` | 基础运行库 |
| `libgl1`, `libegl1` | Qt 的 OpenGL/EGL 后端（pyqtgraph 绘图） |
| `libxkbcommon0`, `libxkbcommon-x11-0` | 键盘布局（xcb 平台插件） |
| `libxcb1`, `libxcb-cursor0`, `libxcb-icccm4`, `libxcb-image0`, `libxcb-keysyms1`, `libxcb-randr0`, `libxcb-render-util0`, `libxcb-shape0`, `libxcb-xinerama0`, `libxcb-xkb1` | Qt xcb 平台插件（窗口、光标、快捷键） |
| `libxi6` | XInput（Qt 输入） |
| `libdbus-1-3`, `libglib2.0-0t64 \| libglib2.0-0` | Qt 事件循环/系统集成 |
| `libfontconfig1`, `libfreetype6` | 字体渲染 |

`Recommends: fonts-noto-cjk` —— 没装中文字体时中文会显示成方框（功能不受影响）。

构建期依赖（只在打包机上）：

```bash
sudo apt-get install -y python3-venv dpkg-dev
python3 -m venv venv
venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  PySide6-Essentials==6.12.0 pyqtgraph==0.14.0 pymavlink==2.4.50 \
  pyserial==3.5 numpy==2.5.4 PyYAML==6.0.3 pyinstaller
```

## 2. 构建

```bash
cd apmlog                      # 工程根
PY=$PWD/venv/bin/python bash packaging/build_deb.sh
# 产物：release/APMLogViewer-<版本>-amd64.deb 与同名 .sha256
```

`packaging/build_deb.sh` 做三件事：PyInstaller（`packaging/apmlogviewer-linux.spec`）
→ 组装 `/opt/apmlogviewer`、`/usr/bin/apmlogviewer`、桌面项与图标 → `dpkg-deb --build`。
版本号取自 `apmlog/__init__.py`，不在这里另写一份。

## 3. 安装 / 卸载

```bash
# 推荐：apt 会自动补齐 Depends
sudo apt install ./release/APMLogViewer-0.1.2-amd64.deb

# 或者手工
sudo dpkg -i release/APMLogViewer-0.1.2-amd64.deb
sudo apt -f install          # 有缺依赖时补装
```

启动：应用菜单里的 **APMLogViewer**，或终端

```bash
apmlogviewer                 # 空窗口
apmlogviewer /path/to/log.BIN
```

卸载：

```bash
sudo apt remove apmlogviewer     # 保留用户缓存/视图记忆
sudo apt purge apmlogviewer      # 连同包内文件一起清掉（用户缓存仍在 ~/.cache、~/.config）
```

用户数据位置：`~/.cache/APMLogViewer`（解析缓存、瓦片缓存）、
`~/.config/APMLogViewer`（设置与视图记忆）。可选环境变量：
`APMLOG_CACHE`（缓存根目录）、`APMLOG_TILECACHE`（瓦片缓存）。

## 4. 自检（无显示器可用）

```bash
QT_QPA_PLATFORM=offscreen apmlogviewer --selftest /path/to/log.BIN --png /tmp/shot.png
# 报告写到 /tmp/shot_selftest.txt，地图页截图 /tmp/shot_map.png
```

## 5. 与 Windows 包的差异

| 项 | Windows | Ubuntu |
| --- | --- | --- |
| 分发形态 | 安装包 + 绿色 zip | `.deb`（同为内置运行库的 onedir 产物） |
| 安装位置 | `%LOCALAPPDATA%\Programs\APMLogViewer` | `/opt/apmlogviewer`（root 所有） |
| 入口 | `APMLogViewer.exe` | `/usr/bin/apmlogviewer` → `/opt/apmlogviewer/APMLogViewer` |
| 构建规格 | `packaging/apmlogviewer.spec` | `packaging/apmlogviewer-linux.spec` |
| 控制台 | 窗口程序 | `console=True`（`--selftest` 输出直接进终端，GUI 启动无额外窗口） |
| 桌面集成 | 开始菜单/快捷方式 | `.desktop` + hicolor 图标 + `postinst` 刷新缓存 |

## 6. 已知限制

- **未做代码签名/公证**；安装时 apt/dpkg 不会拦，但企业环境可能有软件源策略。
- 系统级安装需要 **sudo**；构建机上该账号没有免密 sudo，所以本次验证采用
  `dpkg --root=<临时根> --force-depends -i` 的“假根安装 + 从安装目录直接运行”，
  文件布局、维护脚本与真实安装一致，但未写入真实 `/var/lib/dpkg`。
- 包比较大（约 63 MB，安装后约 224 MB），因为内置了 Python 3.12 与 Qt 6.12 运行库。
- 串口下载日志需要把用户加入 `dialout` 组（`sudo usermod -aG dialout $USER`，重新登录生效）。
- MAVFTP / U 盘模式只在 Windows 真机上做过协议验证；Linux 侧只跑过 FakeFC 回环，
  未接真实飞控。
- 地图瓦片需要外网；Wayland 会话下若窗口异常可 `QT_QPA_PLATFORM=xcb apmlogviewer`。
- 高 DPI 缩放沿用 Qt 默认策略，未针对 200% 以上缩放单独调过。
