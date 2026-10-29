# 示例插件

`example_plugin.py` 把七类挂载点各用了一次，可以直接照抄改。

## 安装（不用重编 exe）

1. 把 `example_plugin.py` 拷到 `%LOCALAPPDATA%\APMLogViewer\plugins\`
2. 主窗工具栏 →「插件」→「重新扫描并重载」

也可以把插件放在任意目录：插件页「添加目录…」加一次即可（会写进设置）。

## 插件能做什么

| 挂载点 | 方法 | 说明 |
| --- | --- | --- |
| 自定义告警规则 | `ctx.register_alert_rule(fn)` | `fn(store)` 返回若干条告警字典，合并进「告警」页 |
| 虚拟/派生通道 | `ctx.register_synthetic_channel("SYN.X", fn, label=, unit=)` | 通道树里出现「插件通道」组，双击即可画 |
| 预设视图 | `ctx.register_preset(Preset(...))` | 出现在「预设视图」下拉里，标「（插件）」 |
| 曲线右键工具 | `ctx.register_plot_tool(name, cb)` | `cb(ctx, selection)`；返回文本会弹窗 |
| 地图自定义图层 | `ctx.register_map_layer(layer)` | `layer.render(store)` 返回线/点/圈三种图元 |
| 独立标签页 | `ctx.register_tab(title, widget)` | 传一个 `QWidget` |
| 导出器 | `ctx.register_exporter(name, fn)` | `fn(store, path)`；返回字符串时宿主负责写文件 |
| 界面主题 | `ctx.register_theme(ThemeSpec(...))` | 四色 + 可选亮/暗曲线档；设置里可直选，`ctx.apply_theme(id)` 可自切 |

只读数据在 `ctx.store`（`meta/stats/types/channels/series/envelope/events/params/modes/tracks`，
全是快照，写不回去）；控制宿主用 `ctx.control`；收事件用 `ctx.signals`。

主题另见 `theme_plugin.py`：`ThemeSpec` 只要四个颜色（`window/base/text/accent`），
写成 `red`、或漏掉一个角色，都会被拒；主题不可用时宿主会**自动回退内置默认主题**并只停用这个插件。

## 出错会怎样

插件初始化抛异常 → 该插件标记「加载失败」，错误详情在插件页「查看错误…」里，
其它插件和主程序照常。运行时回调抛异常 → 记一次错误，同一插件累计 5 次自动停用。


