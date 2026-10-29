# MAVLink 日志下载语义实测笔记（H 工作流）

> 本文所有结论均来自**本机真实数据与真实代码行为**，不是文档记忆。未验证的点会明确标注"未验证"。

## 1. 本机 pymavlink 2.4.50 的 dialect 实测

| 调用 | 结果 |
|---|---|
| `mavutil.set_dialect('ardupilotmega')` | ✅ 可用（导入预生成模块） |
| `mavutil.set_dialect('common')` | ✅ 可用 |
| `mavutil.set_dialect('python')` | ❌ `FileNotFoundError: ...message_definitions\v1.0\python.xml` |
| `mavutil.set_dialect('v20')` | ❌ `ModuleNotFoundError: pymavlink.dialects.v10.v20` → 回落到 mavgen 再 FileNotFoundError |

关键事实：
- `site-packages/message_definitions/` **只有 `v1.0/`** 目录（含 ardupilotmega.xml/common.xml 等，没有 `python.xml`，也没有 `v2.0/`）。
- `mavutil.set_dialect()` 只有在环境变量 `MAVLINK20` 存在时才去找 `dialects.v20.*`，否则找 `dialects.v10.*`。
- 因此本工程**不使用** `mavutil.mavlink_connection(dialect='python')` 这条路（任务书里说的坑就是这个），而是直接：

```python
from pymavlink.dialects.v20 import ardupilotmega as D   # 预生成，v2 线格式，可解 v1 帧
mav = D.MAVLink(port, srcSystem=255, srcComponent=190)  # 注意参数名是 srcSystem/srcComponent
```

- `D.MAVLink_message._pack()` 源码（第 278–308 行）确认 CRC 算法：
  `x25crc(msgbuf[1:])` 再 `accumulate(struct.pack("B", crc_extra))` —— **只追加 1 个 crc_extra 字节**；
  v2 还会**剥离 payload 尾部 0 字节**（变长帧），所以 `LOG_ENTRY` 实际帧长可能小于字段总长。
- 本工程在接收侧**自己校验 CRC**（pymavlink 的 `MAVLINK_IGNORE_CRC` 默认不报错），坏帧计入 `crc_errors` 后丢弃。

## 2. Mission Planner `.tlog` 的真实分帧（重要副产品）

对 `2026-10-07 08-42-34.tlog`（0.30MB）做 CRC 严格校验遍历：

```
valid frames 6109 | distinct ids 41 | 帧前 8 字节大端 µs 时间戳命中 6109 / 不匹配 0
top: AHRS2 517, ATTITUDE 517, VFR_HUD 517, HEARTBEAT 263, SYS_STATUS 258, ...
```

结论：**MP 的 `.tlog` = 每帧前面挂 8 字节大端 µs 时间戳**（不是裸 MAVLink 流）。

分帧长度（用 pymavlink 实打包 + tlog 首帧双重核对）：

```
v1: fe 09 00 01 01 00 00 00 00 00 02 03 00 04 03 d0 14      # 17B = len + 8
    magic len seq sys comp msgid <payload len B> crc2
    → msgid 在**索引 5**（payload 之前），不是 payload 之后
v2: fd 09 00 00 00 01 01 00 00 00 ... 4a d7                  # 21B = len + 12
    magic len seq sys comp flags crc8?  → msgid 在**索引 7..9**（小端 3 字节）
tlog 首帧：fe 06 01 ff be 42 02 00 00 00 02 01 8c dd        # msgid=0x42=66 REQUEST_DATA_STREAM
```

先前一处表述有误（"v1 = 头 5 字节 + payload + msgid + CRC2"）已按上面实测字节修正；本工程关心的
LOG_* 帧全部是 v2(0xFD)，因此第 3 节的结论不受这个更正影响。
另外纠正另一处：先前"这份 tlog 里有 8 帧 LOG_DATA"是用 `len+9` 的错位遍历得到的**假阳性**，
严格遍历的结果是这份小 tlog 里 **LOG_* 帧为 0**，真正的下载会话在 150MB 的那份里（第 3 节）。
（v1 内核只做只读解析；本工程运行期不依赖 tlog。）

## 3. 真实日志下载会话的字段证据

取 `2026-10-07 15-51-45.tlog`（150.0MB，含一次完整飞控日志下载），CRC 严格校验后：

```
validated: {'LOG_REQUEST_LIST': 1, 'LOG_ENTRY': 131, 'LOG_REQUEST_DATA': 358, 'LOG_DATA': 1290836}
```

### LOG_REQUEST_LIST（GCS→FC）
```python
{'target_system': 1, 'target_component': 1, 'start': 0, 'end': 65535}
```
→ 用 `end=0xFFFF` 表示"全部"。

### LOG_ENTRY（FC→GCS），131 条
```python
{'id': 1,   'num_logs': 131, 'last_log_num': 131, 'time_utc': 315532800, 'size': 819200}
{'id': 2,   'num_logs': 131, 'last_log_num': 131, 'time_utc': 315532800, 'size': 915116}
{'id': 3,   'num_logs': 131, 'last_log_num': 131, 'time_utc': 315532800, 'size': 449633}
{'id': 4,   'num_logs': 131, 'last_log_num': 131, 'time_utc': 315532800, 'size': 581612}
```
要点：
- **本固件的 LOG_ENTRY 只有 5 个字段**：`id / num_logs / last_log_num / time_utc / size`。
  任务书里提到的 `num_blocks / cmd_offset / address_offset / length` **在这份 Copter 4.7 会话里没有出现在可解字段中**，
  因此下载逻辑**不依赖块数与地址偏移**，只用 `size`（字节总数）。
- `time_utc = 315532800` = 1980-01-01，是**未设置 RTC 的占位值** → 不能直接当创建时间用，代码里必须判定并回落到本地时间。
- `size` 不一定是 90 的整数倍（915116 % 90 = 56）→ 尾块是不完整帧，落盘按 `size` 截断。

### LOG_REQUEST_DATA（GCS→FC），358 条
```python
{'target_system': 1, 'target_component': 1, 'id': 125, 'ofs': 0,         'count': 4294967295}
{'target_system': 1, 'target_component': 1, 'id': 125, 'ofs': 1207800,   'count': 180}
{'target_system': 1, 'target_component': 1, 'id': 125, 'ofs': 1382220,   'count': 180}
{'target_system': 1, 'target_component': 1, 'id': 125, 'ofs': 2017710,   'count': 270}
```
### LOG_DATA（FC→GCS），1 290 836 条
```
(id, ofs, count, 实到 data 长度) 前 12 条:
(125, 0, 90, 90) (125, 90, 90, 90) (125, 180, 90, 90) (125, 270, 90, 90) ... 严格 +90 递增
ofs 范围: 0 -> 30244860
每帧 data 长度分布: [(90, 1290836)]
帧间隔: 常见 0ms（连发），约每 10~11 帧出现 1.008ms
```

**决定性结论：`ofs` 是字节偏移，不是块号。**
- 若 ofs 是块号，`2017710` 块 ≈ 181MB，但该日志字节偏移最大值只有 30,244,860 B（≈29MB，与 LOG_ENTRY.size 同量级），矛盾；
- 按字节解释则完全自洽：请求 `count=180/270`（= 2/3 个 90 字节块）→ 返回对应数量、`ofs` 逐个 +90 的 LOG_DATA。
- 一次请求可要求多块（count 为字节数，90 的倍数）；也存在 `count=0xFFFFFFFF` 的"整段流式"请求。

本工程据此实现：**请求以字节 ofs + count=blocks*90 分块**（默认每请求 20 块 = 1800B），
并保留 `Addressing.BLOCK` 备选模式（**未在真机验证**，仅供其他固件/新语义试验）。

### LOG_REQUEST_END
本次会话中 **0 条**（Mission Planner 没有发）。故 `send_end` 默认关闭，可显式打开。

## 4. 由此确定的实现约束

1. 列表阶段：`LOG_REQUEST_LIST(start=0, end=0xFFFF)`，收齐 `num_logs` 条 `LOG_ENTRY` 或超时结束。
2. 下载阶段：只以 `size` 为目标字节数；每请求 `blocks*90` 字节；按 `ofs` 定位写入（乱序/重复帧都能正确落位）。
3. 完整性判定：`已收字节覆盖 [0, size)` 全部区间 + 尾部按 `size` 截断；**不信任**任何块数字段。
4. 断点续传：`.part` + 旁车 `.json`（记录 `log_id/size/chunk/已完成区间/统计`）。
5. 命名：`%08d.BIN` 取 `LOG_ENTRY.id`（与 ArduPilot `NNNNNNNN.BIN` 习惯一致）；
   `time_utc` 为 1980 占位时，日期只写进文件名旁的元数据而不是伪造时间戳。

## 5. 本机依赖与运行期限制（实测）

| 项 | 实测结论 |
|---|---|
| **pyserial 未安装** | `import serial` → `ModuleNotFoundError`。串口分支已写好（`SerialTransport`，`serial_for_url` 支持 `COM5` 与 `/dev/ttyACM0`），但**没有装库就无法打开 COM 口**。本工作流**没有擅自安装依赖**，报错信息里给出安装命令：`.\.venv\Scripts\python.exe -m pip install pyserial`。因此**串口链路至今未经真机验证**。 |
| `fastcrc` 已存在 | `fastcrc 0.5.0` 在 venv 里，`D.x25crc` 走 C 加速路径，CRC 逐帧校验不构成瓶颈。 |
| **UDP 启动死锁** | 飞控侧 UDP 传输在收到第一个包之前**没有对端地址可发**；而 GCS 若没收到心跳就不敢发定向请求。解法：`udp/udpout` 链路自动开启 **GCS 周期心跳**（`MavLinkSession(send_our_heartbeat=True)`），这也是标准 GCS 行为。TCP 无此问题。 |
| `LOG_REQUEST_END` | 真实会话里出现 0 次（Mission Planner 没发），故默认不发，可用 `--send-end` 显式打开。 |
| v1/v2 混线 | 同一链路里 v1(0xFE) 与 v2(0xFD) 帧都会出现（tlog 首帧即 v1 `REQUEST_DATA_STREAM`），分帧器两种都按 CRC 严格解。 |
| 无真硬件 | 以上全部在 **FakeFC（本机 TCP/UDP/内存回环）+ 真实 .BIN** 上验证；**尚未在 Pixhawk 实机或数传模块上跑过**，串口波特率/流控行为、4.6.x 老固件的 LOG_ENTRY 扩展字段仍需上机确认。 |

---

# 真机部分（2026-10-17，COM91 实飞控）

设备：`COM91` = `ArduPilot (COM91)`，**VID:PID 1209:5741**（USB CDC，原生 USB，非 UART 桥）。
`pyserial 3.5`。probe：`sysid=1 compid=1 type=2 autopilot=3 base_mode=81 armed=False`。
固件（下载回来的日志里）：`ArduCopter V4.6.3 (12437a26)` / `ChibiOS: 88b84600` / 板卡 `CORVON743V2`。

真机 `LOG_REQUEST_LIST` 回 5 条，`complete=True`，用时 **0.09s**：

| id | size(B) | 块数(90B) | time_utc | 说明 |
|---|---|---|---|---|
| 1 | 2320667 | 25786 | 1791429604 | 2026-10-08 03:20:04 UTC |
| 2 | 757760 | 8420 | 1792115416 | 2026-10-16 01:50:16 UTC |
| 3 | 971771 | 10798 | 1792116592 | 2026-10-16 02:09:52 UTC |
| 4 | 446464 | 4961 | 1792116798 | 2026-10-16 02:13:18 UTC |
| 5 | 721562 | 8018 | **315532800** | RTC 未设置（1980 占位，与 tlog 结论一致） |

→ **4.6.3 的 `LOG_ENTRY` 字段与 4.7.0 完全相同**（`id/num_logs/last_log_num/time_utc/size`），
`time_utc` 有真值的会给出真值，没设置 RTC 的仍是 `315532800` 占位——之前"不可信 RTC"的判定成立。

## 6. 参数调优（`blocks_per_request` × `window`）

### 6.1 单样本法不可靠（先记录踩坑）

每组合只下 120KB 即取消，交错 3 轮，KB/s：

| blocks/window | 三轮 | 中位 | 抖动 |
|---|---|---|---|
| 20/2 | 15, 36, 36 | 36.1 | 60% |
| 40/2 | 158, 38, 181 | 158.2 | 90% |
| 40/4 | 37, 182, 33 | 37.2 | **402%** |
| 60/4 | 21, 39, 38 | 37.8 | 47% |
| 80/2 | 46, 46, 47 | 46.3 | 2% |
| 120/1 | 60, 66, 65 | 64.6 | 8% |
| 120/4 | 39, 64, 65 | 64.0 | 40% |

速率呈 **35–180 KB/s 双峰**，同一组合能差 5 倍（飞控侧 SD 读/主循环调度抖动）。
**结论：短样本不能用来定参数**，必须整段下载 + 多轮中位数。

### 6.2 整段下载 id4（446464B），6 组合 × 3 轮交错

| blocks/window | 中位秒 | 最快 | 最慢 | 中位 KB/s | 最差 KB/s | 请求数 | 重传 | 重复帧 | CRC | 完成 |
|---|---|---|---|---|---|---|---|---|---|---|
| **20/2（旧默认）** | **30.84** | 28.54 | 30.98 | **14.1** | 14.1 | 249 | 3 | 0 | 0 | 3/3 |
| 40/2 | 17.58 | 2.68 | 17.85 | 24.8 | 24.4 | 125 | 2 | 0 | 0 | 3/3 |
| 40/4 | 5.72 | 5.54 | 20.25 | 76.3 | 21.5 | 125 | 10 | 0 | 0 | 3/3 |
| 80/2 | 10.57 | 10.35 | 10.61 | 41.2 | 41.1 | 63 | 3 | 0 | 0 | 3/3 |
| **120/1（新默认）** | **7.50** | 7.46 | 7.59 | **58.1** | 57.4 | 42 | **0** | 0 | 0 | 3/3 |
| 120/4 | 12.82 | 12.60 | 12.83 | 34.0 | 34.0 | 42 | 18 | 0 | 0 | 3/3 |

### 6.3 单请求上限探测（同一条 id4，2 轮）

| blocks/window | 中位秒 | 中位 KB/s | 请求数 | 重传 | 重复帧 | CRC |
|---|---|---|---|---|---|---|
| 120/1 | 7.51 | 58.0 | 42 | 0 | 0 | 0 |
| 160/1 | 6.87 | 63.5 | 31 | 1 | 1 | **1** |
| 200/1 | 5.57 | 78.3 | 22 | 6 | **152** | **5** |
| 240/1 | 5.79 | 75.3 | 18 | 7 | **380** | **6** |
| 120/2 | 7.92 | 55.0 | 42 | 2 | 0 | 0 |

### 6.4 真机推荐默认值：`blocks_per_request=120, window=1`

- 依据：**7.51s / 58.0 KB/s，重传 0、重复帧 0、CRC 错 0**，两轮 7.51/7.52s（抖动 0.1%）。
- 相比旧默认 `20/2`（30.84s / 14.1 KB/s）**快 4.1×**；与用户独立实测的 14.9 KB/s 吻合（同一档位）。
- `40/4` 中位更快（5.72s）但**双峰**（2.68–20.25s）且 10 次重传，不适合当默认。
- **`window>1` 在真机上是负收益**：120/1=7.51s → 120/2=7.92s → 120/4=12.82s（18 重传）。
  飞控是单线程按请求顺序吐 90B 帧，多个在途请求只会互相挤占，并在 `timeout_chunk=2.5s` 上触发重传。
  （此前 window>1 的优势只在 FakeFC 本地回环里成立，那不是调优目标。）
- **单请求不要超过 160 块**：≥200 块开始出现重复帧与 CRC 错帧（固件一次吐太多，链路/缓冲溢出）。
- 已同步落到：`download_log()` 默认参数、`cli link fetch --blocks/--window` 默认、`LinkPanel` 的
  每请求块数默认 120（上限 160）与 worker `window=1`。

### 6.5 USB CDC 上波特率确实无意义

| 写法 | 解析结果 | 行为 |
|---|---|---|
| `COM91` | `kind=serial device=COM91 baud=57600` | 正常：列表 5 条、下载逐字节一致 |
| `serial:COM91` | 同上（baud=57600） | 同上 |
| `serial:COM91?baud=57600` | 同上 | 同上 |
| `serial:COM91?baud=921600` | `baud=921600` | **同样正常**：列表 5 条 `ids=[1,2,3,4,5]`，同参数样本照样完成 |

→ 两种写法解析后**行为一致**（只是 `baud` 字段值不同，传给 `serial.Serial()` 的 `baudrate` 对原生 USB CDC 不生效）。
921600 那次样本测得 25.3 KB/s、57600 那次 196 KB/s，属 6.1 记录的飞控侧抖动，**不是波特率差异**。

### 6.6 GUI 面板打真机（offscreen）

```
[1] 连接：0.94s，connectionChanged = ['open', 'ready']，lbl_state='飞控在线　飞控在线 sysid=1'
[2] 刷新列表：0.36s 收到 logsListed 5 条，表格 5 行，选中第 2 行 -> selected_log_id()=3
[3] 下载 id3：16.25s -> 00000003.BIN 971771B 58.4 KB/s sha1=bdbaae8f4c41303e…，进度条 100%
[4] 换目录重下：16.29s，sha1 与第 3 步完全一致（bdbaae8f4c41303eca3b299bdf596c200e9a5eb9）
[5] 断开：connectionChanged 追加 'closed'，序列 ['open','ready','closed']
[6] 重复开关 3 轮：每轮均 ['open','ready','closed']，列表 5 条，进程存活，退出码 0（无 Qt native abort）
```
期间还收到真机 `STATUSTEXT`：`PreArm: Compass not calibrated`、`PreArm: Battery 1 low voltage failsafe`
（说明 RX 分发与文本解码在真链路上正常；飞控处于未解锁状态）。

**这一轮暴露并修掉两个只有真机才会出现的 GUI 缺陷**：
1. `_do_connect` 打开链路就返回，不等心跳；真机心跳约 1Hz，于是"连接完立刻点刷新"时
   `fc_sysid` 仍是 `None`，`list_logs()` 抛 `LinkError` → 不发 `logs_ready` → UI 永远等不到列表。
   修法：worker 增加 `_wait_for_fc()`（连接后与列表/下载前都等，超时 8~10s 并给出明确提示，
   且失败路径也发终止信号）。
2. 连接成功后 `_busy` 从未清零（只有 lost/closed/error 会清），导致"刷新列表/下载"按钮保持禁用，
   点击成为空操作。修法：`_set_busy(..., op=)` 记录当前操作，`ready`（connect）与列表返回
   （connect/demo）都会解除 busy。
两者在 FakeFC 离线测试里都被绕过（本地回环心跳几乎瞬时 + 测试直接调 `show_logs`），
**是真机把测试盲区照出来了**。

## 7. 只读性硬证据：本端 tx 逐 msgid 计数

方法：`apmlog/link/audit.py::AuditTransport` 包住真实串口传输，把**写上线的字节**用同一套
严格 CRC 分帧器（`endpoint.FrameReader`）反解成帧，逐 msgid 计数；禁发名单按 dialect 的**消息名**
动态匹配（`PARAM_SET / COMMAND_LONG / COMMAND_INT / MISSION_* / FTP / FILE_TRANSFER / FLASH_* /
MAV_INSTALL_* / SET_MODE / COMPONENT_ARM_DISARM / LOG_ERASE …`），不是凭 msgid 记忆。

真机 COM91 三次独立会话（每次都是新开串口，统计为**该会话累计全部发送**）：

| 会话 | 发送的 msgid 与消息名 | 帧数 | 字节 | 禁发命中 | CRC 错帧 | 只读判定 |
|---|---|---|---|---|---|---|
| 调优第 1 轮（列表+15 样本+整段1次） | `LOG_REQUEST_DATA(119)` | 935 | 22440 | 0 | 0 | **通过** |
| | `LOG_REQUEST_LIST(117)` | 1 | 18 | 0 | | |
| | **合计** | **936** | **22458** | | | |
| 调优第 2 轮（重复测量+整段 id2） | `LOG_REQUEST_DATA(119)` | 842 | 20208 | 0 | 0 | **通过** |
| | `LOG_REQUEST_LIST(117)` | 1 | 18 | 0 | | |
| | **合计** | **843** | **20226** | | | |
| 调优第 3 轮（整段 6 组合 × 3 轮） | `LOG_REQUEST_DATA(119)` | 1974 | 47376 | 0 | 0 | **通过** |
| | `LOG_REQUEST_LIST(117)` | 1 | 18 | 0 | | |
| | **合计** | **1975** | **47394** | | | |

要点：

* **整个真机过程只发过 2 种消息**：`LOG_REQUEST_LIST`(msgid 117) 与 `LOG_REQUEST_DATA`(msgid 119)。
  没有 `HEARTBEAT`——串口上本端不自动发心跳（`send_our_heartbeat` 只对 `udp/udpout` 打开，
  那是 UDP 学不到对端地址才需要的），所以比"允许心跳"还更严格。
* **没有** `LOG_REQUEST_END`（118）：默认 `send_end=False`，与 tlog 实测一致。
* **没有** `PARAM_SET`(23)、`PARAM_READ`(20)、`COMMAND_LONG`(76)、`COMMAND_INT`(77)、
  `MISSION_*`、`FTP/FILE_TRANSFER_PROTOCOL`(110)、`FLASH_*`、`MAV_INSTALL_*`、`SET_MODE`(11)、
  `COMPONENT_ARM_DISARM`(179)、`LOG_ERASE`(120) —— 全部为 0 帧。
* 每帧字节数可核对：`LOG_REQUEST_DATA` 24B（v2 帧，payload 14B 去尾零）、`LOG_REQUEST_LIST` 18B。
* 检测能力本身有离线单测保证（不需要真机也能证明"如果发了 PARAM_SET 一定抓得到"）：
  `tests/test_link_endpoint.py::TestTxAudit::test_param_set_is_flagged` 编一条 `PARAM_SET` +
  一条 `COMMAND_LONG` 过同一个审计层，断言两者都进 `forbidden` 且 `is_read_only()` 为假。
* GUI 面板（`LinkPanel`）走的是同一套 `list_logs()/download_log()` 发送函数，未使用其他发送口，
  因此上表的"只有 117/119"同样适用于面板路径。

## 8. 测试健壮性：线程异常逃逸会被"张冠李戴"

现象：`tests/test_link_endpoint.py::TestFrameReader::test_roundtrip_and_garbage_prefix`
在 `/batch` + `/package` 并行时偶发失败，报 `WinError 10054`。
**但该用例根本不开 socket**（纯内存 `FrameReader.feed`）。

真实原因：`FakeFC` 的 `fakefc-tcp` 服务线程在客户端中途关闭连接时，
`_stream_data` 里 `log.error(...)` 之后 **裸 `raise`**，`LinkError(10054)` 一路穿出
`serve_transport` → 线程死亡 → `threading.excepthook`。pytest 把这条 traceback
归给**当时正在运行的用例**，于是纯内存用例背锅；同时产生
`PytestUnhandledThreadExceptionWarning`（全量跑那条 warning 就是它）。

修法（全部在链路层内）：
* `transport.py`：`LinkError` 带 `transient` 标记；新增 `is_transient_socket_error()`
  与 `connect_with_retry()`（10053/10054/10061 重试 3 次、间隔 50ms）；
  `TcpClientTransport` 走重试连接；`TcpServerTransport.accept` 由阻塞改为带 deadline 的超时轮询，
  瞬时错误继续轮询而不是直接抛。
* `fakes.py`：`_stream_data` 改为记 `last_error` 后抛内部 `_ClientGone`；
  `serve_transport` 捕获 `_ClientGone/LinkError` 并安静收尾；`serve_tcp` / `serve_pipe`
  的线程入口再加一层兜底 —— **任何异常都不许逃出线程**。
* `linkpanel.py`：`_LinkWorker.run()` 整层包裹（含 `_cmds.get(timeout=0.5)` 与收尾断开），
  信号发射走 `_emit_safe()`，避免接收方已销毁时 `RuntimeError` 逃出 QThread。
* 回归护栏：`TestThreadContainment` 用 `SO_LINGER(1,0)` **强制 RST**（普通 `close()` 会让对端的写
  全部落进 OS 发送缓冲而不报错，测不到逃逸——本机实测踩过），并临时替换 `threading.excepthook`
  断言"逃逸条数为 0"。把上述四层全部还原成旧写法后，该用例确实报出
  `LinkError('TCP 写失败: [WinError 10054] ...') thread=<Thread(fakefc-tcp, stopped)>`，
  证明护栏不是空跑。
* 真 TCP 回环用例加环境变量开关 `APMLOG_LINK_TCP_TESTS`（**默认开启**，设 `0` 才跳过，
  且跳过原因显式打印，不静默 skip）。
