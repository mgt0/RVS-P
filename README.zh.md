# OMX SCAN CONTROL

[![한국어](https://img.shields.io/badge/lang-한국어-e11d48)](README.md)
[![English](https://img.shields.io/badge/lang-English-2563eb)](README.en.md)
[![中文](https://img.shields.io/badge/lang-中文-f59e0b)](README.zh.md)

![license](https://img.shields.io/badge/license-MIT-22c55e)
![python](https://img.shields.io/badge/python-3.12-3776AB)
![platform](https://img.shields.io/badge/platform-Windows-0078D6)
![tests](https://img.shields.io/badge/tests-passing-16a34a)
![last commit](https://img.shields.io/github/last-commit/mgt0/OMX-FACTORY-SYSTEM?color=f59e0b)
![repo size](https://img.shields.io/github/repo-size/mgt0/OMX-FACTORY-SYSTEM?color=eab308)
![issues](https://img.shields.io/github/issues/mgt0/OMX-FACTORY-SYSTEM?color=8b5cf6)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-ec4899)

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

用 5 个线程分别采集 5 台相机时，各自相位互不相关。若只是收集每台相机的*最新*帧，
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
│       ├── camera_cam1/frame_000000.jpg …
│       ├── camera_cam2/ … camera_cam5/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{cam1..cam5}.jpg + labels.json
    └── defect/<object_id>/{cam1..cam5}.jpg + labels.json
├── background/{cam1..cam5}.jpg   <- "背景采集" 保存的空扫描台参考图
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
| `ts_{cam1..cam5}` | 各相机采集时刻（monotonic） |

加上 `metadata.json` 中的 `monotonic_offset` 即可将 monotonic 还原为 epoch。
（使用 monotonic 的原因：墙上时钟可能因 NTP 校正而回退，不适合帧排序。）

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 相机顺序、缺陷类型、下一个 episode ID |
| GET | `/api/status` | 录制·臂·遥操作·相机的综合状态（主轮询） |
| GET | `/api/ports` | 端口 + 相机状态快照 |
| POST | `/api/ports/refresh` | 清除覆盖后重新扫描 |
| POST | `/api/ports/test` | 实际 open + ping 验证 |
| POST | `/api/ports/identify/{start,confirm}` | 拔插差分两阶段 |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG 流 |
| GET | `/api/snapshot/<name>` | 单张 JPEG |
| POST | `/api/recording/{start,stop}` | 采集控制 |
| GET | `/api/episodes` | 已保存 episode 列表（metadata.json） |
| PATCH | `/api/episodes/<id>` | 修改标签·物体 ID（同时移动分类文件夹） |
| DELETE | `/api/episodes/<id>` | 删除 episode（若为最后引用，同时清理分类图像） |
| POST | `/api/emergency_stop` | 紧急停止 —— 遥操作·全部皮带·录制一并停止 |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,release}` | 传送带（多皮带）控制 |
| GET/POST | `/api/scan/model/<object_id>` | 查询/上传 CAD 基准模型（STL） |
| POST | `/api/scan/background` | 采集空扫描台背景（用于背景差分） |
| POST | `/api/teleop/{start,stop}` | 遥操作 |
| POST | `/api/arm/home` | 回原点（录制中拒绝） |
| POST | `/api/train/{start,stop}` | ACT 训练控制（demo/lerobot 模式） |
| GET | `/api/train/status` | 训练状态 + 指标时间序列（轮询） |
| GET | `/api/train/stream` | 训练指标 SSE 实时流 |
| GET | `/api/scan/objects` | 可重建的扫描物体列表 |
| POST | `/api/scan/reconstruct` | 5 视角 -> 3D 网格（visual hull） |
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
lerobot-train --dataset.repo_id=omx/scan --policy.type=act
```

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
与实际完全一致地得到验证。选择 **LEROBOT 模式** 会引导你以独立进程运行真实训练 ——
用 `scripts/to_lerobot.py` 转换后使用 `lerobot-train` CLI 才是正道（见上文"真实 ACT
训练"一节）。没有理由在握着 5 台相机和串口的服务器进程内跑 GPU 训练。

实时传输使用 **SSE**（Server-Sent Events）。轮询总是滞后一个间隔，而 SSE 在一个
epoch 结束的瞬间图表就会增长。

---

## Rollout —— 用训练好的 ACT 策略自主运行（新增）

按上述流程（`to_lerobot.py` -> `lerobot-train`）训练结束后，可用该检查点自主控制从臂。
训练（`lerobot-train`）刻意运行在本服务器进程之外 —— 无法一边握着 5 台相机和串口，
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
- 通过 `frames` 传入的相机名必须与 `CAMERA_ORDER`（`cam1`–`cam5`）一致 —— 给出训练
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
      启动传送带 3 轴（含 `lm`）+ 模仿学习 rollout 尚未在真机上验证。`hardware.py`
      是两者的公共接触点，若有异常先从那里看。
- [ ] **含基于 CAD 的缺陷检查（`cad.py`）的三路完全集成** —— 传送带与模仿学习本次已
      合并；CAD 检查（分支 `feature/cad-inspection-and-fixes`，最后一次推送为
      2026-08-11 12:05，比 3axis 更旧）尚未纳入本次集成。

---

## 3D 扫描检查

从 5 视角扫描图像生成 3D 形状，用鼠标旋转查看进行检查。

**为什么用 Visual Hull 而非摄影测量。** cam1~cam5 这 5 个视角彼此张开很大，因此像
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
