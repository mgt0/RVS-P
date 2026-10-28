# RVS-P (Robot-Vision Scan Platform)

[![한국어](https://img.shields.io/badge/lang-한국어-e11d48)](README.md)
[![English](https://img.shields.io/badge/lang-English-2563eb)](README.en.md)
[![中文](https://img.shields.io/badge/lang-中文-f59e0b)](README.zh.md)

![license](https://img.shields.io/badge/license-MIT-22c55e)
![python](https://img.shields.io/badge/python-3.12-3776AB)
![platform](https://img.shields.io/badge/platform-Windows-0078D6)
![tests](https://img.shields.io/badge/tests-passing-16a34a)
![last commit](https://img.shields.io/github/last-commit/mgt0/RVS-P?color=f59e0b)
![repo size](https://img.shields.io/github/repo-size/mgt0/RVS-P?color=eab308)
![issues](https://img.shields.io/github/issues/mgt0/RVS-P?color=8b5cf6)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-ec4899)

**机器人·视觉一体化产品检测平台。** 通过模仿学习（LeRobot ACT）学会动作的机械臂把产品放上传送带，
传送带停止后由 4 台相机从侧面和斜上方拍摄，重建 3D 形状，并与标准 CAD（STL）模型比较、标出偏差部位。
机器人、相机与传送带都在同一个 Web 仪表盘中操作。

面向 ROBOTIS OMX-L（主臂）/ OMX-F（从臂）遥操作与 5 相机扫描数据采集的仪表盘。
一次演示即可同时采集 **用于 LeRobot ACT 训练的序列** 和 **用于良品/缺陷 CNN 分类的静态图像**。

---

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) 先在无硬件下检查 UI / 流水线（config.yaml 中 simulate: true）
python run.py
# -> http://localhost:5000

# 2) 连接真实硬件前，先固定一次端口
python scripts/find_ports.py
#   将打印出的 serial_number 写入 config.yaml 的 arms.*.serial_number

# 3) 使用真实硬件运行
OMX_SIMULATE=0 python run.py
```

演示 / 采集时，请使用正式服务器而非开发服务器：

```bash
./scripts/serve.sh
```

> **Windows**：以上命令基于 Linux。虚拟环境用 `.venv\Scripts\activate` 激活，
> 环境变量用 `$env:OMX_SIMULATE="0"` 设置。`serve.sh` 中的 gunicorn 在 Windows 上
> 无法运行，请直接使用 `python run.py`。端口名为 `COM*` 而非 `/dev/ttyACM*`
> （识别策略相同）。

---

## 仪表盘界面

运行 `python run.py` 后打开 http://localhost:5000。界面遵循高性能 HMI（ISA-101）原则：

- 正常状态用中性灰，颜色只用于异常和操作控件。
- 每个状态都同时用颜色、符号（● ○ ■ ▲ ⓘ）和文字表示。
- 以桌面屏幕（宽 1280px 以上）为准。

| 界面 | 功能 |
|---|---|
| 概览 | 工位示意图、告警、相机 fps、机械臂关节、同步偏差、数据准备度、最近一次检测拍摄 |
| 设备控制 | 双皮带传送带（速度 %·方向·闸门）、遥操作、回零、相机实时画面 |
| 采集 | 示范录制会话（用于 ACT）、检测样本拍摄与标注、episode 管理 |
| 训练 | 跟读 LeRobot 训练日志（loss·l1·kld·grdn·lr·吞吐·GPU）、演示曲线 |
| 检测 | 5 相机拍摄 → 3D 形状与表面异常（橙色）、判定依据与可信度、CAD 基准模型 |
| 诊断 | 运行环境、串口识别、相机设置、安全限制、相机组标定 |

- **顶栏**
  - 右上角是**急停**按钮，即使 Vue 或 CDN 加载失败也能使用。
  - 自动循环的开始/停止按钮和料筐状态也在顶栏（见下文“自动循环”一节）。
  - 点击告警标签会打开详情窗口，说明问题是什么、影响、原因和解决方法。
- **前端结构**
  - 无需构建：Vue 3（CDN，版本固定）加上 `frontend/static/ui/` 下的静态文件。
  - 每个界面对应一个文件 `js/screens/<名称>.js`，共享状态和组件在 `js/core/`。
- **没有硬件时查看界面**，使用隔离的模拟服务器：

  ```bash
  python scripts/dev_sim.py --port 5100 --data .dev_data/demo
  ```

  - 会拒绝 5000/5001 端口。
  - 不会改动真实的 `lerobot_data` 和抓取轨迹文件。

---

## 为什么这样设计

### 端口识别 —— 三级回退

`/dev/ttyACM*` 编号按 USB 枚举顺序分配。重启或重新插拔线缆后，主臂与从臂会**互换**。
在这种状态下启动遥操作，从臂会充当主臂，可能损坏硬件。

| 优先级 | 方法 | 备注 |
|---|---|---|
| 1 | `serial_number` 匹配 | 最稳健。设置一次即永久稳定 |
| 2 | 拔插差分 | 等同 `lerobot-find-port`。仪表盘的 `IDENTIFY` 按钮 |
| 3 | `port_hint` 手动指定 | 最后手段 |

失败时异常会携带 `role`，因此仪表盘会**明确显示是哪只臂**。

### 相机同步 —— 为什么不用"最新帧"

用 4 个线程分别采集 4 台相机时，各自相位互不相关。若只是收集每台相机的*最新*帧，
当采样周期与采集周期相同时（都是 30Hz），在边界处有的相机持有新帧、有的持有上一帧，
从而**相差一个周期（33ms）**（混叠）。

解决：为每台相机设置 8 帧环形缓冲区，然后
1. 取各相机最新时间戳的**中位数**作为公共基准时刻
   （不用平均值，是为了在某台相机停顿时不把基准拉偏），
2. 从每个环形缓冲区中选取**最接近**基准时刻的帧。

实测结果（仿真，30Hz × 5 台）：

| | 改进前 | 改进后 |
|---|---|---|
| skew 中位数 | 32ms | **7.2ms** |
| 超过 15ms 的帧 | 76% | **3.6%** |

剩余的 skew 不会被隐藏，而是逐帧记录在 `joint_states.parquet` 中，
因为训练阶段需要能够过滤掉超过阈值的区间。

### 安全限制

写入从臂之前，**始终**分两级钳制：

1. 绝对软限位（`joint_limits`）—— 位于硬件限位之内
2. 每步相对变化量（`max_relative_target`）—— 即使主臂跳变，从臂也缓慢跟随

回原点时不会一次性写入目标值，而是按 `homing_speed_deg_per_s` 插值发送。
一次性写入会使电机以最大速度猛冲。服务器关闭时，`atexit` 按 回原点 -> 释放扭矩 ->
关闭端口 的顺序清理；即使回原点失败，也由 `finally` 保证扭矩释放。

### 传送带驱动 —— DC 电机 + HW-095（L298N 双 H 桥模块）

一块 STM32F411 BlackPill 控制两条皮带（M1 供料 = A 通道，M2 作业 = B 通道）和分拣闸门伺服（PB3）。
没有编码器，所以不知道转速和位置；速度只能用 PWM 占空比（%）设定。LM 导轨（M3）已取消。

| L298N | M1（供料，A） | M2（作业，B） |
|---|---|---|
| ENA/ENB（PWM） | **PB0**（固件常量 `PB0_ALT1`，TIM3_CH3） | PA3（TIM5_CH4） |
| IN1/IN3 | PA1 | PA6 |
| IN2/IN4 | PA2 | PA7 |

- **不要使用 PA0。** 它是板载 KEY 按键，不能作为输出（所以 M1 的 ENA 用 PB0）。
- 电机电源 12V 接 L298N 的 12V 端子，GND 与 BlackPill 共地。
- **拔掉 ENA/ENB 跳线。** 插着时始终全速，PWM 不起作用。
- 建议在 EN、IN 引脚加 10kΩ 下拉电阻 —— 复位和 DFU 期间引脚悬空，电机可能抖动。
- 固件常量（都需要现场实测）：`MIN_RUN_DUTY` 20（电机真正能转的最低占空比，临时值）、PWM 2kHz/10bit、`RAMP_MS` 300。
- **主机看门狗。** 电机运行中固件 2 秒（`HOST_TIMEOUT_MS`）收不到任何一行就停掉所有电机；端口被关闭（DTR 拉低）时立即停止。
  只要有一条皮带在转，服务器就用每个端口一个线程每 0.5 秒发一次 `PING`（与命令共用同一把锁）。服务器崩溃或 `release` 后皮带会停。
- `STOP` 为刹车，`DISABLE` 为自由滑行停止。`DISABLE`（包括紧急停止）之后固件会拒绝 `START`，直到 `ENABLE`（"驱动器上电"）。
  `DUTY 0` 等同于 `STOP`，设定速度保留。默认速度为 `config.yaml` 的 `conveyors.*.default_duty`（默认 40），连接后在第一次 `START` 之前发送一次。
- `config.yaml` 里残留的旧键 `conveyors.lm`、`stroke_mm`、`mm_per_rev`、`cycle.belt_rpm` 只记录警告并被忽略。

### 存储 —— 两个消费者，两个 Writer

ACT（序列）与 CNN（单张快照）的需求完全不同。混在一个类里会导致条件分支爆炸，
因此在 `DatasetWriter` 接口之后分离（策略模式）。

CNN 侧只保留**一组代表性数据**，而非整个 episode。若把所有序列帧都用作分类数据，
会塞入数千张几乎相同的图像，导致过拟合和 train/val 泄漏。

中断的 episode 在 `abort()` 中整个目录删除。只写了一半的 episode 是最糟糕的数据 ——
它会悄悄侵蚀训练质量。

---

## 数据结构

```
lerobot_data/
├── episodes/
│   └── episode_000042/
│       ├── camera_cam2/frame_000000.jpg …
│       ├── camera_cam2/ … camera_cam5/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{cam2..cam5}.jpg + labels.json
    └── defect/<object_id>/{cam2..cam5}.jpg + labels.json
├── background/{cam2..cam5}.jpg   <- "背景采集" 保存的空扫描台参考图
└── models/<object_id>.stl        <- "注册基准模型" 保存的 CAD
```

`joint_states.parquet` 各列：

| 列 | 说明 |
|---|---|
| `frame_index` | 从 0 递增 |
| `timestamp` | 自 episode 开始起的经过秒数 |
| `sync_skew_ms` | 该帧集内相机间的时间偏差 |
| `leader_joint_{0..4}` | 主臂关节角度（deg） |
| `follower_joint_{0..4}` | 从臂关节角度（deg） |
| `ts_{cam2..cam5}` | 各相机采集时刻（monotonic） |

加上 `metadata.json` 中的 `monotonic_offset` 即可将 monotonic 还原为 epoch。
（使用 monotonic 的原因：墙上时钟可能因 NTP 校正而回退，不适合帧排序。）

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 相机顺序、缺陷类型、下一个 episode ID |
| GET | `/api/status` | 录制·臂·遥操作·相机的综合状态（主轮询）。包含 `sync_skew_ms`（相机间同步偏差，单位 ms，相机少于 2 台时为 null）、`demo`（演示会话状态）和 `cycle`（自动循环状态） |
| GET | `/api/ports` | 端口 + 相机状态快照 |
| POST | `/api/ports/refresh` | 清除覆盖后重新扫描 |
| POST | `/api/ports/test` | 实际 open + ping 验证 |
| POST | `/api/ports/identify/{start,confirm}` | 拔插差分两阶段 |
| GET | `/api/alerts[?refresh=1]` | 当前需要人知晓的事项（含原因·影响·解决方法，严重度 fault/warn/info）。5 秒缓存，`refresh=1` 跳过缓存 |
| GET | `/api/diagnostics[?refresh=1]` | 环境、串口、相机设置、安全限值（关节限位、最大相对位移、回原点速度 `homing_speed_deg_per_s` 等）诊断。30 秒缓存，`refresh=1` 跳过缓存（实时连接状态见 `/api/ports`） |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG 流 |
| GET | `/api/snapshot/<name>` | 单张 JPEG |
| POST | `/api/recording/{start,stop}` | 采集控制（演示会话期间 `start` 和 `stop`、自动循环期间 `start` 返回 409 —— 会话线程持有录制，手动停止介入会丢失该 episode。紧急停止不受此限制） |
| POST | `/api/demo/start` | 开始演示录制会话 —— body 为 `object_id`、`task`、`episode_sec`（1~600）、`reset_sec`（0~120）。须已开启遥操作、两臂已连接，且没有录制或 rollout 在运行 |
| POST | `/api/demo/{next,redo,end}` | 保存并进入下一条 / 丢弃并重录（仅录制中） / 保存并结束会话。进度见 `/api/status` 的 `demo`。演示 episode 以 `kind=demo` + `task` 保存，不生成分类图像 |
| GET | `/api/episodes` | 已保存 episode 列表（metadata.json） |
| PATCH | `/api/episodes/<id>` | 修改标签·物体 ID（同时移动分类文件夹；`kind=demo` 的演示 episode 不动分类文件夹） |
| DELETE | `/api/episodes/<id>` | 删除 episode（若为最后引用，同时清理分类图像；演示 episode 不计入引用） |
| GET | `/api/inspections?limit=` | 3D 检测拍摄列表（最新在前）及标签概况。`capture_id` 为 `YYYYmmdd_HHMMSS`，同一秒内的拍摄会加 `_02`、`_03` … 后缀 |
| GET | `/api/inspections/<capture_id>/<cam>.jpg` | 拍摄原图 |
| POST | `/api/inspections/<capture_id>/label` | 保存拍摄标签 —— 只写入 `inspections/<id>/label.json`（不触碰 `classification/`） |
| DELETE | `/api/inspections/<capture_id>` | 删除拍摄（仅限无标签的，有标签则返回 400） |
| GET | `/api/objects` | 物体列表（来自 episode、已标注的拍摄、CAD 模型）及建议拍摄姿态数 |
| GET | `/api/readiness` | 数据准备度（ACT episode 数、良品/缺陷物体数、目标值） |
| POST | `/api/emergency_stop` | 紧急停止 —— 自动循环·遥操作·全部皮带·录制·演示会话一并停止 |
| POST | `/api/emergency_reset` | 只重新使能被紧急停止关掉的皮带驱动器（"驱动器上电"，什么都不会动）。只针对已连接的皮带 —— 已断开或已 release 的皮带不会被重新打开端口，而是报告为 `not_connected` |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,enable,servo,release}` | 传送带（2 条 DC 电机皮带）控制（自动循环期间除 `stop`、`enable(false)` 外的驱动命令返回 409）。`speed` 的 body 为 `{"duty": 0~100}`（PWM 占空比 %，超出范围返回 400）。运行中的 `dir`、驱动器关闭时的 `start` 会被固件拒绝（400）。状态键：`running`、`duty`（设定速度）、`direction`、`enabled`、`servo_deg`（仅 M1） |
| GET | `/api/cycle/status` | 自动循环状态 —— 步骤·进度消息·判定结果·篮子计数·能否开始（`ready`：阻止启动的原因 `reasons`，不阻止的提示 `notes`）。与 `/api/status` 的 `cycle` 键相同 |
| POST | `/api/cycle/start` | 开始一次循环（抓取 → 皮带输送 → 检测 → 分拣）。未就绪或已在运行则返回 400 + 原因。检查通过后会在机械臂动作之前先打开皮带端口，打不开则返回 400 |
| POST | `/api/cycle/stop` | 立即中止 —— 只停皮带和自主运行（闸门·机械臂停在原地）。state 变为 `aborted` |
| POST | `/api/cycle/baskets/empty` | 清空篮子后把计数归零 —— body 为 `{"basket": "good"}`、`"defect"`、`"all"` 之一 |
| GET/POST | `/api/scan/model/<object_id>` | 查询/上传 CAD 基准模型（STL） |
| POST | `/api/scan/background` | 采集空扫描台背景（用于背景差分；自动循环期间返回 409 —— 皮带上的物体会被烙进背景） |
| POST | `/api/scan/capture` | 在传送带静止的此刻拍一组并做 3D 判定 —— body 为 `settle_sec`（0~3）、`resolution`（16~96）。不是数字返回 400，自动循环期间返回 409。拍摄原图保存在 `inspections/<capture_id>/` |
| POST | `/api/teleop/{start,stop}` | 遥操作（自动循环期间 `start` 返回 409） |
| POST | `/api/arm/home` | 回原点（录制中或自动循环期间拒绝） |
| POST | `/api/train/{start,stop}` | ACT 训练控制（demo/lerobot 模式） |
| GET | `/api/train/status` | 训练状态 + 指标时间序列（轮询） |
| GET | `/api/train/stream` | 训练指标 SSE 实时流 |
| GET | `/api/train/runs/latest` | `logs/train` 中最近一次训练运行的摘要（没有则 `run: null`，不会开始跟读） |
| GET | `/api/scan/objects` | 可重建的扫描物体列表 |
| POST | `/api/scan/reconstruct` | 5 视角 -> 3D 网格（visual hull）。body 为已保存的物体（`object_id`、`label`）或已保存的检测拍摄（`capture_id` —— 读取 `inspections/<id>/<cam>.jpg`，按与 `/api/scan/capture` 相同的方式重建）。只做计算，所以自动循环期间也允许 |
| POST | `/api/scan/reconstruct_demo` | 无数据即时查看用的合成重建 |

错误响应全部为同一形态：

```json
{ "ok": false, "error": { "code": "arm_port_not_found",
  "message": "未找到 LEADER 臂端口…", "detail": { "role": "leader" } } }
```

---

## 真实 ACT 训练 —— LeRobot 转换

本仓库的目录结构是为人阅读而设计的，并非标准 LeRobot 数据集。训练前需转换一次。

```bash
python scripts/to_lerobot.py --repo-id omx/scan
python scripts/train_act.py --repo-id omx/scan --steps 20000
```

`scripts/train_act.py` 是把 `lerobot-train` 作为子进程启动的包装器（会自动加上 `--policy.type=act`、
必需的 `--policy.repo_id` 和 `--policy.push_to_hub=false`）。包装器不认识的训练参数写在 `--` 之后
（`... --repo-id omx/scan -- --policy.device=cuda --save_freq=5000`）。`--log-freq`（默认 50）会明确传给
lerobot，它就是仪表盘曲线上点的间隔。每次运行，包装器都会在 `logs/train/<YYYYmmdd_HHMMSS>/` 下写入：
`run.json`（参数、状态 RUNNING/DONE/FAILED、returncode、output_dir）、`train.log`（lerobot 输出原文）、
`metrics.jsonl`（解析后的指标）、`gpu.jsonl`（GPU 状态，见下）和 `heartbeat`（每 10 秒只刷新修改时间的空文件 ——
服务器据此判断包装器是否还活着，见下），并原样输出到控制台。同一秒内启动的运行，文件夹名会加 `_02`、`_03` …
后缀（两位数，所以按名称排序 = 按时间排序）。要停止训练，请在启动包装器的终端按 Ctrl+C。
数据结构与日志解析器在 `backend/core/train_log.py`。没有 lerobot 时，包装器会给出安装说明并立即失败。

训练期间，包装器每 5 秒用 `nvidia-smi` 测一次 GPU 状态写入 `gpu.jsonl`（只用子进程，不用 torch），
LEROBOT 模式的仪表盘把最近的摘要（约 30 秒）显示为"GPU"卡片。`nvidia-smi` 不存在时只提示一次并立即停止采样；
暂时性的失败会在下一个周期重试，连续失败 5 次才停止。无论哪种情况，训练都照常进行。

转换的关键在于观测/动作的对应关系。

| LeRobot 字段 | 我方列 | 含义 |
|---|---|---|
| `observation.state` | `follower_joint_*` | 机器人当前所处的姿态 |
| `action` | `leader_joint_*` | 人所指令的姿态 |
| `observation.images.<cam>` | `camera_<cam>/frame_*.jpg` | 同一时间步的帧 |

若两者互换，策略会学到"当前姿态 -> 当前姿态"，损失下降得很漂亮，但机器人什么都不做。
`tests/test_episodes.py` 固定了这个对应关系。

meta/*.jsonl、分块与 stats 聚合不手写，而是交给
`LeRobotDataset.create/add_frame/save_episode`。反正训练需要 lerobot，
手写的格式在 lerobot 版本升级时会悄悄错位。

---

## ACT 训练指标（实时）

ACT 是预测关节角度的**回归**模型，所以分类的"准确率"并不直接适用。因此用三个指标量化。

| 指标 | 含义 | 为什么需要 |
|---|---|---|
| **L1 Loss** | 预测误差绝对值的均值 | 优化是否真的在推进 |
| **各关节 MAE（deg）** | 每个关节平均差几度 | 诊断哪个关节没学好 |
| **Success@τ** | 所有关节都在 τ 度以内的时间步比例 | 对应分类的"准确率" —— "这种程度能抓起来吗" |

Success@τ 只在**所有关节同时**处于阈值以内时才计为成功。因为现实中只要有一个关节
差得很远，那个动作就是失败。

**DEMO 模式** 在无 GPU、无数据集的情况下重现真实的训练曲线（loss 指数衰减 + success
上升），可立即查看仪表盘。此时数值同样通过真正的 `compute_metrics()`，因此图表逻辑
与实际完全一致地得到验证。**LEROBOT 模式** 不启动训练：它跟读 `scripts/train_act.py`（见上文"真实 ACT
训练"一节）留下的最近一次 `logs/train/*` 运行，在同一张图表上画出真实的 loss 曲线（`LeRobotTrainer`，
0.5 秒轮询）。没有理由在握着 4 台相机和串口的服务器进程内跑 GPU 训练，所以训练始终在服务器之外，
服务器重启后它依然存活。没有运行记录时会说明包装器的用法并以 FAILED 结束。

LEROBOT 模式下的不同之处：
- x 轴是 **STEP** 而不是 EPOCH。lerobot 日志里的 `step:1K` 是有损压缩（1000、1200、1400 都显示为 `1K`），
  所以不用解析值，而是按"第 n 条指标行 × log_freq"还原。用 `--resume` 接续的运行，x 值会偏移检查点的步数。
- lerobot 不在日志里记录 val MAE 和成功率，对应的 KPI·图表显示为"—"或隐藏。VAL loss 只在存在
  `step N: eval_loss=…` 行时才会画出。同一步的评估行会并入那一步的点（来两次也只有一个点）；没有指标行的
  步会成为单独的点 —— 接上上一次的 train loss，而早于第一条指标行到达的评估行则以空的 train loss 保留。
  因此 x 轴上不会出现两次相同的步。
- **STOP 只是停止"跟读"**，不会杀死训练进程（服务器不是训练的父进程）。包装器被强制终止时，`run.json` 仍为
  RUNNING，但 heartbeat 文件停止更新超过 60 秒后，服务器就把该运行视为 FAILED（没有 heartbeat 的旧运行用 STOP 结束）。

实时传输使用 **SSE**（Server-Sent Events）。轮询总是滞后一个间隔，而 SSE 在一个
epoch 结束的瞬间图表就会增长。

---

## Rollout —— 用训练好的 ACT 策略自主运行（新增）

按上述流程（`to_lerobot.py` -> `lerobot-train`）训练结束后，可用该检查点自主控制从臂。
训练（`lerobot-train`）刻意运行在本服务器进程之外 —— 无法一边握着 4 台相机和串口，
一边占用 GPU 数小时（见 `backend/core/act_trainer.py` 的 `LeRobotTrainer`）。相反，
**rollout（推理）运行在本进程内** —— 因为 `HardwareHub` 已经握着从臂/相机。相关代码为
`backend/core/lerobot_bridge.py`（`ActInferenceRunner`，仅推理）与
`backend/core/rollout.py`（`AutoRunLoop`，与 `TeleopLoop` 对称结构）—— 即使没有这
两个文件（即未安装 lerobot），其余仪表盘也照常启动。

```bash
pip install -r requirements.txt -r requirements-lerobot.txt   # 仅 rollout 负责人
```

```
POST /api/rollout/start   {"checkpoint": "outputs/train/<job>/checkpoints/<step>/pretrained_model"}
POST /api/rollout/start   {"waypoints": "lerobot_data/waypoints/episode_000001.json", "speed": 1.0}
POST /api/rollout/stop
GET  /api/rollout/status
```

**路点回放（无需训练）** —— 如果任务只需要重复同一个动作，就不需要 ACT 训练和 GPU。
把录制的 episode 提取为角度列表并原样回放：

```bash
python scripts/to_waypoints.py --episode 000001          # --tol 调整稀疏度（默认 1.5 度）
```

输出是人可读、可用文本编辑器修改的 JSON，回放是确定性的。
`WaypointRunner`（`backend/core/waypoints.py`）与 `ActInferenceRunner` 具有相同的
`predict()` 签名，因此原样复用 `AutoRunLoop`（watchdog · SafetyGuard · 停止路径）——
没有专用于回放的控制循环。它不看相机（`needs_frames = False`），到达轨迹末端时自行
停止。`/api/rollout/status` 会附加 `progress_sec` / `total_sec`。若臂落后目标超过 5
度，回放时钟会暂停（到达门控），以防第一个路点较远时整条轨迹被跳过。

- `checkpoint` 或 `waypoints` 二者必居其一。对于 `checkpoint`，由于训练是独立进程，
  本服务器无从得知哪个检查点是最新的 —— 直接使用 `lerobot-train` 日志最后一行打印
  的路径。
- teleop 与 rollout 都是调用 `follower.write()` 的循环，因此**互斥**。一个在运行时，
  另一个拒绝启动。`SafetyGuard`（软限位 + 相对变化量钳制）对 rollout 的应用与 teleop
  完全相同。
- 通过 `frames` 传入的相机名必须与 `CAMERA_ORDER`（`cam2`–`cam5`）一致 —— 给出训练
  时未见过的相机名，策略会悄悄忽略或报错。

### 在 Windows 上实际遇到的 4 个问题（全部复现并解决）

在 Windows 上用真实硬件把本仓库从头到尾（teleop -> 录制 -> `to_lerobot.py` 转换 ->
`lerobot-train` -> rollout）跑通验证的过程中，实际遇到了以下 4 个问题。lerobot/torch
版本变化时可能再次遇到，故保留诊断顺序。

1. **torch DLL 初始化失败**（`WinError 1114`，`c10.dll` 加载失败）。若 cv2/pandas
   先于 torch 加载，会先占用同名的运行时 DLL（OpenMP 系），破坏 torch 侧的 DLL
   初始化。已在 `run.py` 和 `scripts/to_lerobot.py` 顶部放了一个什么都不做的
   `import torch`（失败也忽略）来抢占该槽位 —— 如果新增入口点（脚本），要加同样的
   模式。
   - 若症状是这个，且单独 `import torch` 可以但只在脚本中崩溃，那 100% 是这个问题。
     用 `python -c "import cv2,pandas; import torch"` 复现确认。
2. **缺少 lerobot extras**：仅 `pip install lerobot` 会同时缺失 `lerobot.datasets`
   （需 `pip install 'lerobot[dataset]'`，内部使用 HuggingFace `datasets` 包）与
   `lerobot-train`（需 `pip install 'lerobot[training]'`，需要 `accelerate`）。
   `requirements-lerobot.txt` 已用 `lerobot[dataset,training]` 反映。
3. **缺少 `--policy.repo_id` 时 `lerobot-train` 立即报错**（`ValueError: 'repo_id'
   argument missing`）。即使无意上传到 hub，这个参数也是必需的。同时传
   `--policy.push_to_hub=false` 就只保存在本地。
4. **关节数不匹配导致 rollout 推理立即失败**（`tensor a(6) must match tensor
   b(5)`）。即使主/从臂物理上关节数相同，若 `config.yaml` 的 `arms.*.joint_ids`
   数量不同（我方情况：主臂读 5 个，从臂含夹爪读 6 个），则 `to_lerobot.py` 生成的
   数据集的 `observation.state` 维度与实际 `follower.read()` 长度不一致。
   `ActInferenceRunner` 会直接从检查点的 `config.json` 读取
   `input_features["observation.state"]["shape"]` 并自动裁剪匹配，但若要真正训练抓取
   （夹爪操作），必须把夹爪轴也包含进 `arms.leader.joint_ids` 并重新采集 —— 当前配置
   下只学习臂的位置，夹爪开合根本不在数据里。
5. **直接调用 `PolicyProcessorPipeline.from_pretrained()` 会破坏 postprocessor**
   （`EnvTransition must be a dictionary. Got Tensor`）。该类的 `to_transition` 默认
   固定为仅字典（`batch_to_transition`），因此也会应用到必须接收动作（张量）的
   后处理器上。务必通过 `lerobot.policies.make_pre_post_processors(policy_cfg=
   policy.config, pretrained_path=checkpoint_path)`（与官方 `lerobot_eval.py` 所用
   相同）加载 —— 该函数会显式地给后处理器传入
   `to_transition=policy_action_to_transition`。

> 上述第 4 项已于 2026-08-11 通过在 `arms.leader.joint_ids` 中加入夹爪（ID 6）解决
> —— 现在主/从臂都读 6 个关节。但安全配置（`safety.joint_limits`、
> `arms.follower.home_position`）目前仍只有 5 个值，因此夹爪轴实际上可以无限制运动
> —— 见下文"后续工作"。

### `feature/integration` 合并备注（2026-08-14）

将 `feature/3axis-stepper-soft-pulse`（传送带 3 轴步进电机集成，1 块 STM32 BlackPill +
3 个 TB6600，切换为软脉冲）与 `feature/lerobot-rollout`（本节全部 —— 模仿学习流水线）
合并到 `feature/integration` 分支。**base 为 3axis 侧** —— 按用户指示，端口号、相机
匹配、传送带配置等"设备接线相关的值"全部原样采用 3axis 分支的值。模仿学习相关代码
（`lerobot_bridge.py`、`rollout.py`、`act_trainer.py`、`to_lerobot.py`、
`requirements-lerobot.txt`）原样取自 lerobot-rollout —— 这部分 3axis 未改动，无冲突。

冲突仅在 `config.yaml` 与 `run.py` 两处：
- `config.yaml` 的 `arms.leader/follower`：**port_hint 保留 3axis 的值（COM6/COM7）**，
  并额外叠加 lerobot-rollout 侧用 `scripts/find_ports.py` 确认的 `serial_number` ——
  serial_number 的匹配优先级高于 port_hint，因此即使端口变化也安全，且不与 port_hint
  策略冲突。
- `run.py`：torch 优先 import（规避 DLL 顺序问题，lerobot-rollout）与单实例锁 + 流
  端口分离（3axis）是不同的关注点，只是按正确顺序拼接起来。

**因合并而自动解决的部分**（3axis 分支自身已填好的值）：
- `safety.joint_limits` 中夹爪（第 6 个）的范围 —— 宽松地开到 `[-180, 180]`，靠基于
  电流模式的电流限制防止过度夹紧。**角度本身并未实测**，因此夹爪是否真的安全，务必
  在首次运行时以低速/近距观察确认。
- `arms.follower.home_position` 中夹爪（第 6 个）的值 —— 由 3axis 侧填为 `0.0`。
  **该值对本个体实际夹爪是否为安全的原点角度尚未验证** —— 在集成分支上首次回原点时
  请有人在旁观察。

**尚未完成（与合并无关、仍遗留的 TODO）**：
- [ ] **含夹爪的模仿学习首次验证** —— 录制 1~2 个 episode（含夹爪开合动作，`--repo-id`
      指定为不与现有 `omx/scan` 重叠的新名 —— 关节数不同，混用会使 `to_lerobot.py`
      崩溃）-> 转换 -> `lerobot-train` -> 通过 `/api/rollout/start` 确认。目前整条
      流水线只用 5 关节（不含夹爪）的数据验证过。
- [ ] **重新实测 rollout `rate_hz`** —— 请求 5Hz 时观测到的 tick 数比预期多（很可能
      是工具调用延迟导致的测量误差，代码本身看起来没问题）—— 以真实墙上时钟为准再测
      一次。
- [ ] **合并后的真机集成测试** —— `pytest`（仿真）全部通过，但在同一服务器进程中同时
      启动传送带 2 条皮带（已换成 DC 电机）+ 模仿学习 rollout 尚未在真机上验证。`hardware.py`
      是两者的公共接触点，若有异常先从那里看。
- [ ] **含基于 CAD 的缺陷检查（`cad.py`）的三路完全集成** —— 传送带与模仿学习本次已
      合并；CAD 检查（分支 `feature/cad-inspection-and-fixes`，最后一次推送为
      2026-08-11 12:05，比 3axis 更旧）尚未纳入本次集成。

---

## 3D 扫描检查

从 5 视角扫描图像生成 3D 形状，用鼠标旋转查看进行检查。

**为什么用 Visual Hull 而非摄影测量。** cam2~cam5 这 4 个视角彼此张开很大，因此像
COLMAP 这类 SfM 的特征点匹配几乎必然失败（通常需要数十至数百张有大量重叠的图像）。
改用**基于轮廓的 space carving**：

1. 在每个视角提取物体轮廓（前景掩膜），
2. 将 3D 体素网格的每个体素投影到全部 5 个视角，
3. 只保留**在每个视角都位于轮廓内**的体素 -> 物体的凸近似形状。

凹陷的内部（孔洞）无法复原，但对确认形状、标注缺陷位置已足够，而且最重要的是**仅凭
5 个视角就总能工作**。体素经 marching cubes 得到表面网格（无 scikit-image 时回退为
体素立方体）。

**检查显示。** 将 CNN 判定结果（good/defect + 置信度）以 KPI 显示，缺陷部位以**日落
橙的顶点颜色**叠加在网格表面。无需单独标记，直接在表面可见。当前缺陷位置为演示值，
实际中把 CNN Grad-CAM 或异常检测热图的 3D 反投影位置作为 `defect_regions` 传入即可。

**查看器操作**：拖拽 = 旋转，滚轮 = 缩放。基于 three.js r128；为减少依赖，未用
OrbitControls 而是直接实现了一个最小轨道控制。

> **无数据即时查看**：按下 3D SCAN INSPECTION 区域的 **DEMO** 按钮，即可在无相机、
> 无物体的情况下查看合成形状 + 橙色高亮。

---

## 自动循环 —— 一个按钮 = 抓取 → 输送 → 检测 → 分拣

点一下顶部的 [▶ 开始循环] 就运行一次循环（制作报告 3.1 的流程图）。服务器线程按下面的顺序推进，进度从
`/api/status` 的 `cycle` 键（或 `GET /api/cycle/status`）轮询。实现在 `backend/core/cycle.py`。

1. **抓取** —— 闸门置于关闭角度并设定皮带方向·速度后，用路点回放（与 `/api/rollout/start` 相同的
   autorun）抓起物体放到皮带上。若在 `pick_timeout_sec` 内没有结束或出现 fault，则失败。轨迹结束后再等
   `pick_settle_sec`（默认 1 秒）才进入输送 —— 避免夹爪放下物体的动作还没结束皮带就动了（需现场实测调整）。
2. **输送** —— 皮带转 `to_scan_sec` 后停下。
3. **检测** —— 用与 `/api/scan/capture` 相同的函数（`run_scan_capture`）：确认静止 → 拍摄 → 3D 判定。拍摄原图
   保存在 `inspections/<capture_id>/`。拍摄失败则循环也失败。
4. **分拣** —— 良品：皮带转 `good_run_sec` 后停下。缺陷品：转 `defect_to_branch_sec` 停下后打开闸门，
   等待 `gate_hold_sec` 再关上。然后对应篮子的计数 +1。

判定：`stats.quality.level == "low"` 判为缺陷（无法判定的物体绝不送往良品线）；
`anomaly.flagged_ratio >= defect_ratio` 判为缺陷；其余为良品。

**制作抓取路点（第一次）**

1. 在设备控制中开启遥操作，把"抓起并放到皮带上"的演示录成一个 episode。从待机姿态开始并回到该姿态 ——
   循环之间没有回原点，下一次循环从上一次结束时的姿态开始。
2. `python scripts/to_waypoints.py --episode <ID> --out lerobot_data/waypoints/pick_place.json`
3. 让 `config.yaml` 的 `cycle.pick_waypoints` 指向该文件（默认值就是上面的路径）。同一 `cycle:` 段里的抓取后等待
   （`pick_settle_sec`，0~60 秒）、皮带时间·速度、闸门角度、判定标准、篮子容量都只是起点 —— 每行都标有"需现场实测"。
   值不合法（超出范围、不是数字）时，服务器启动时就会直接拒绝。

**安全规则**

- 启动前检查（`ready`）只要有一项不通过就不会开始，并给出原因（`reasons`）：抓取动作文件（仓库内的路径显示为
  相对仓库的路径）、循环皮带存在于配置中且设有端口·驱动器已使能·处于静止、从臂已连接、至少 2 台重建用相机、
  没有遥操作/rollout/录制/演示在运行、篮子有余量。设置了 `pick_checkpoint`（ACT）会拒绝启动 —— 它没有完成信号，
  抓取总会以超时结束（请用路点；是在检查中阻止而不是配置校验，所以服务器照常启动）。
  - 实机上服务器刚启动时皮带端口是关着的（第一条命令时才打开）。`ready` 既不打开端口也不阻止 —— 只在 `notes`
    里提示"开始时会打开皮带端口"。检查通过后，`POST /api/cycle/start` 会在**机械臂动作之前**打开端口；打不开则
    返回 400 并给出原因，机械臂不会动。
  - 紧急停止会关闭皮带驱动器（DISABLE）。固件会拒绝 DISABLE 状态下的 START，所以紧急停止之后必须由人在设备控制里用
    "驱动器上电"（`POST /api/emergency_reset`）明确解除，才能开始循环。端口关闭期间被关掉的驱动器，会在开始时
    打开端口之后确认，并以同样的理由拒绝。
- 循环运行期间，服务器会以 409（`cycle_busy`）拒绝：开始遥操作/rollout/录制/演示、手动拍摄（`/api/scan/capture`、
  `background`、`calibrate/shot`）、皮带驱动命令（start·speed·dir·servo·release）和回原点。
  循环中途拍背景会把皮带上的物体烙进背景，导致之后的判定全部出错。只做计算的 `reconstruct`、`calibrate/solve`
  不受阻止。皮带 `stop`·紧急停止·`/api/cycle/stop` 始终可用。**人只要停住任何一条皮带或解除其驱动器，循环也会
  一起中止**（避免循环在下一步又把人刚停下的皮带转起来）。两个轴共用伺服闸门和 COM 端口，所以循环期间的手动
  操作一律保守地阻止，连停另一条皮带也会让循环停下。
- 中止或出错时只停皮带和 autorun（皮带优先 —— 停 autorun 可能要等 hub 锁）。闸门和机械臂停在原地（闸门在下一次
  循环开始时回到关闭角度）。紧急停止最先切断循环，并稍等（最多 1 秒）已经发出的皮带命令结束，然后才停止并关闭
  皮带驱动 —— 这样已经通过检查的 START 不可能插到 DISABLE 之后把驱动器重新打开。让机械臂动作的 autorun 启动，
  会在 hub 自己的锁内再次确认是否中止·服务器是否关闭，停止结束之后不会再启动。服务器关闭并关掉皮带端口之后，
  不会再重新打开。所有等待都是 `Event.wait`，所以停止在等待途中也会立即生效。
- 篮子满了（`basket_capacity`）就不会开始。清空后按 [已清空]（`POST /api/cycle/baskets/empty`），计数归零。
  计数只存在于服务器内存中，重启后为 0。
- **第一次运行时请调低 `pick_speed`，并站在机器人旁边观察。**

---

## 已知限制

**浏览器并发连接上限。** HTTP/1.1 限制每主机 6 个连接（MDN）。若 5 个 MJPEG 流占用
其中 5 个，轮询 API 就会挨饿。把预览降到 12fps 可缓解，但如需更多，请在前面放一个
HTTP/2 反向代理，或把流分离到单独的主机名。

**Flask 开发服务器。** 流每个连接占用一个 worker。`use_reloader=False` 是必需的
（reloader 会把进程启动两次，打开相机两次并锁定设备）。实际使用请用
`scripts/serve.sh` 的 gunicorn + gevent 配置。

**软件同步。** 没有硬件触发，因此无法把 skew 降到 0。若需要更高精度，需要支持外部
触发的工业相机。

**USB 带宽。** 把 5 台相机接到一个主机控制器上，即使 640x480@30 不用 MJPG 也不够。
请保持 `fourcc: MJPG`，并尽可能用单独的 PCIe USB 卡分散负载。

---

## 故障排查

| 症状 | 检查 |
|---|---|
| arm 一直 OFFLINE | `groups $USER` 中是否有 `dialout`。没有则 `sudo usermod -aG dialout $USER` 后重新登录 |
| 相机 NO SIGNAL | 用 `v4l2-ctl --list-devices` 确认路径。是否被其他进程占用：`fuser /dev/video0` |
| fps 只有目标值的一半 | USB 带宽不足。确认 `fourcc: MJPG`，分散到多个 hub |
| skew 警告频繁 | 把 `sample_rate_hz` 设得低于相机 fps（例如相机 30 -> 采样 20） |
| 关节 ID 无响应 | 用 Dynamixel Wizard 确认 ID / 波特率 |

---

## 测试

```bash
pytest
```

无硬件即可运行 35 个测试 —— 安全钳制、路径遍历防御、状态转移、同步回归、
ACT 回归指标（success@τ 边界）、3D 重建、缺陷叠加验证。

---

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
