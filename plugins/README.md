# 插件分支（`plugins`）

本分支**只放 APMLogViewer 的插件**，与 `main` 上的应用源码分开管理：

- `main` —— 应用本体（代码、打包、文档）
- `plugins`（本分支）—— 可直接使用的插件；新增或更新插件只提交到这里，不动 `main`

仓库：<https://github.com/KonuriMaki2004/APMLogViewer>（公开）

## 目录里现在有什么

| 文件 | 作用 |
| --- | --- |
| `plugins/插件开发指南.md` | **完整开发指南（873 行）**：五分钟上手、八个挂载点逐个讲、`ctx.store/control/signals` 用法、拒绝条件、FAQ、可整段拷走的示例 |
| `plugins/example_plugin.py` | 示例插件：七类挂载点各用一次（告警规则、派生通道、预设视图、曲线右键工具、地图图层、独立标签页、导出器），可直接照抄改 |
| `plugins/theme_plugin.py` | 主题插件示例：`ThemeSpec` 四色（`window/base/text/accent`），可带亮/暗曲线档 |
| `plugins/installed/ai_assistant/ai_assistant.py` | **本机已安装插件存档**：AI 智能日志识别助手（顶部「AI识别」入口 + 独立标签页、多 API 账号与多模型切换），独立目录存放，不与示例插件混放 |

`example_plugin.py` / `theme_plugin.py` 与 `main` 分支 `plugins-samples/` 下的同名文件逐字节一致，`plugins-samples/` 是文档说明，本分支是**可直接下载使用**的存放点。

`plugins/installed/<插件 id>/` 用来归档实际在用的插件，按插件各占一个子目录，不和根目录的示例插件混在一起（插件管理器只扫描目录顶层的 `.py` 与含 `__init__.py`／`plugin.py` 的包目录，所以 `installed/` 下的存档不会被重复加载）。

## 安装（不用重编 exe）

1. 把 `.py` 文件拷到 `%LOCALAPPDATA%\APMLogViewer\plugins\`
   （通常是 `C:\Users\<用户名>\AppData\Local\APMLogViewer\plugins`，目录不存在就自己建）
2. 主窗工具栏 →「插件」→「重新扫描并重载」

其它放法（任选其一）：

- 「插件」页 →「添加目录…」，指向本仓库 `plugins/` 目录（会写进设置，重启仍生效）
- 环境变量 `APMLOG_PLUGIN_DIRS`，多个目录用分号分隔

## 写一个插件

- 插件 id = 文件名（或包目录名），以 `_` 或 `.` 开头的文件/目录会被跳过
- 必须提供 `register(ctx)`；可选 `PLUGIN_NAME`、`__version__`
- 包形式：目录里放 `__init__.py`（或 `plugin.py`）作为入口
- 数据只读：`ctx.store`（`meta/stats/types/channels/series/envelope/events/params/modes/tracks`，全是快照，写不回去）
- 控制宿主：`ctx.control`；接收事件：`ctx.signals`；日志：`ctx.log`
- 当前插件 API 版本 `API_VERSION = 1`，宿主版本 0.1.3

挂载点：

| 挂载点 | 方法 |
| --- | --- |
| 自定义告警规则 | `ctx.register_alert_rule(fn)` |
| 虚拟/派生通道 | `ctx.register_synthetic_channel("SYN.X", fn, label=, unit=)` |
| 预设视图 | `ctx.register_preset(Preset(...))` |
| 曲线右键工具 | `ctx.register_plot_tool(name, cb)` |
| 地图自定义图层 | `ctx.register_map_layer(layer)` |
| 独立标签页 | `ctx.register_tab(title, widget)` |
| 导出器 | `ctx.register_exporter(name, fn)` |
| 界面主题 | `ctx.register_theme(ThemeSpec(...))` |

## 出错会怎样（不是崩溃）

- 插件初始化抛异常 → 只把**这一个**插件标成「加载失败」，主程序与其它插件照常；错误详情在插件页「查看错误…」
- 运行时回调抛异常 → 记一次错误，同一插件累计 5 次自动停用
- 主题不可用时宿主**自动回退内置默认主题**，只停用该主题插件

## 提交约定

- 一个插件一个文件，或一个自包含的包目录；文件名用小写 + 下划线
- **不引入新的第三方依赖**：只能用宿主已装的 PySide6、pyqtgraph、numpy、pymavlink、pyserial、PyYAML
- 遵守只读原则：插件不得尝试向飞控写参数或发指令（宿主本身也不提供写接口）
- 主题插件必须给全 `window/base/text/accent` 四个颜色，缺一个会被拒绝
- 新插件请带上文件头注释：用途、作者、版本、依赖的 API 版本
