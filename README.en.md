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

**A robot-vision product inspection platform.** A robot arm trained by imitation learning
(LeRobot ACT) places a part on the conveyor; when the belt stops, four cameras capture it
from the sides and above, reconstruct its 3D shape, and compare it against the reference
CAD (STL) model to highlight deviating regions. The robot, cameras, and conveyor are all
operated from a single web dashboard.

A dashboard for ROBOTIS OMX-L (leader) / OMX-F (follower) teleoperation plus 5-camera
scan data collection. A single demonstration collects both a **sequence for LeRobot ACT
training** and **still images for a good/defect CNN classifier** at the same time.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) Check the UI/pipeline first, without hardware (simulate: true in config.yaml)
python run.py
# -> http://localhost:5000

# 2) Before connecting real hardware, pin the ports once
python scripts/find_ports.py
#   Write the printed serial_number into arms.*.serial_number in config.yaml

# 3) Run with real hardware
OMX_SIMULATE=0 python run.py
```

For demos / data collection, use a real server instead of the dev server:

```bash
./scripts/serve.sh
```

> **Windows**: the commands above are for Linux. Activate the venv with
> `.venv\Scripts\activate` and set env vars with `$env:OMX_SIMULATE="0"`. The gunicorn
> in `serve.sh` does not run on Windows, so use `python run.py` directly. Port names are
> `COM*` instead of `/dev/ttyACM*` (the identification strategy is the same).

---

## Dashboard screens

Open http://localhost:5000 after `python run.py`. The UI follows high-performance HMI (ISA-101) principles:

- Normal states are shown in neutral greys. Colour is used only for abnormal states and for controls.
- Every state combines colour, a symbol (● ○ ■ ▲ ⓘ) and text.
- The layout is designed for desktop screens (1280px and wider).

| Screen | What it does |
|---|---|
| Overview | Station map, alerts, camera fps, arm joints, sync skew, data readiness, latest inspection capture |
| Equipment | 2-belt conveyor (speed %, direction, gate), teleoperation, homing, live camera feeds |
| Collect | Demonstration recording sessions (for ACT), inspection-sample capture and labelling, episode management |
| Train | Follows the LeRobot training log (loss, l1, kld, grdn, lr, throughput, GPU), demo curve |
| Inspect | 5-camera capture → 3D shape and surface anomalies (orange), evidence and confidence, CAD reference |
| Diagnostics | Runtime environment, serial port identification, camera settings, safety limits, rig calibration |

- **Top bar**
  - **Emergency stop** sits at the top right. It works even if Vue or the CDN fails to load.
  - The auto-cycle start/stop controls and basket status are also here (see [Auto cycle](#auto-cycle--one-button--pick--carry--inspect--sort)).
  - An alert chip opens a detail dialog: what is wrong, its impact, the cause and how to fix it.
- **Frontend structure**
  - Build-free Vue 3 (CDN, pinned version) plus static files under `frontend/static/ui/`.
  - Each screen is one file, `js/screens/<name>.js`. Shared state and components live in `js/core/`.
- **To try the UI without hardware**, use the isolated simulation server:

  ```bash
  python scripts/dev_sim.py --port 5100 --data .dev_data/demo
  ```

  - It refuses ports 5000 and 5001.
  - It never touches the real `lerobot_data` or pick trajectory.

---

## Why it is built this way

### Port identification — a 3-step fallback

`/dev/ttyACM*` numbers are assigned in USB enumeration order. Reboot or replug the cable
and the leader and follower **swap**. Starting teleoperation in that state makes the
follower act as the leader and can damage the hardware.

| Rank | Method | Notes |
|---|---|---|
| 1 | `serial_number` match | Most robust. Set once, stable forever |
| 2 | Unplug diff | Same as `lerobot-find-port`. The dashboard's `IDENTIFY` button |
| 3 | `port_hint` manual override | Last resort |

On failure the exception carries the `role`, so the dashboard shows **exactly which arm**
is affected.

### Camera synchronization — why not "latest frame"

Capturing 4 cameras each on its own thread leaves their phases uncorrelated. Just
collecting each camera's *latest* frame means that when the sampling period equals the
capture period (both 30 Hz), at the boundary some cameras hold a new frame and others the
previous one — **off by one period (33 ms)** (aliasing).

Fix: give each camera an 8-frame ring buffer, then
1. take the **median** of the cameras' latest timestamps as the common reference time
   (not the mean, so a stalled camera doesn't drag the reference), and
2. from each ring buffer, pick the frame **closest** to the reference time.

Measured (simulation, 30 Hz x 5 cameras):

| | Before | After |
|---|---|---|
| Median skew | 32 ms | **7.2 ms** |
| Frames over 15 ms | 76% | **3.6%** |

The remaining skew is not hidden — it is recorded per frame in `joint_states.parquet`,
because training needs to be able to filter out spans that exceed a threshold.

### Safety limits

Before writing to the follower, **always** clamp in two stages:

1. Absolute soft limit (`joint_limits`) — inside the hardware limit
2. Per-step relative delta (`max_relative_target`) — even if the leader jumps, the
   follower follows gradually

Homing does not write the target in one shot; it interpolates it at
`homing_speed_deg_per_s`. Writing it at once makes the motor snap at full speed. On
server shutdown, `atexit` cleans up in the order home -> release torque -> close port,
and even if homing fails, torque release is guaranteed by `finally`.

### Conveyor drive — DC motors + HW-095 (L298N dual H-bridge module)

One STM32F411 BlackPill drives two belts (M1 feed = channel A, M2 work = channel B) and the sort-gate
servo (PB3). There is no encoder, so RPM and position are unknown; speed is set only as PWM duty (%).
The LM guide (M3) is gone.

| L298N | M1 (feed, A) | M2 (work, B) |
|---|---|---|
| ENA/ENB (PWM) | **PB0** (firmware constant `PB0_ALT1`, TIM3_CH3) | PA3 (TIM5_CH4) |
| IN1/IN3 | PA1 | PA6 |
| IN2/IN4 | PA2 | PA7 |

- **Do not use PA0.** It is the onboard KEY button and must not be an output (that is why M1 ENA is PB0).
- Motor supply 12 V goes to the L298N 12 V terminal; tie GND to the BlackPill's GND.
- **Remove the ENA/ENB jumpers.** With them fitted the motor is always at full speed and PWM has no effect.
- 10 kΩ pull-downs on the EN and IN pins are recommended — the pins float during reset/DFU and the motors can jerk.
- Firmware constants (all need on-site measurement): `MIN_RUN_DUTY` 20 (lowest duty at which the motor really turns, provisional), PWM 2 kHz / 10 bit, `RAMP_MS` 300.
- **Host watchdog.** If a motor is running and the firmware receives no line for 2 s (`HOST_TIMEOUT_MS`) it stops all
  motors, and it stops immediately when the port is closed (DTR drop). While any belt runs the server sends `PING`
  every 0.5 s from one thread per port (same lock as commands). If the server dies or the port is released, the belts stop.
- `STOP` brakes; `DISABLE` free-runs to a stop. After `DISABLE` (including an emergency stop) the firmware refuses
  `START` until `ENABLE` ('Enable drivers'). `DUTY 0` equals `STOP` and the set speed is kept. The default speed is
  `conveyors.*.default_duty` in `config.yaml` (default 40), sent once before the first `START` after connecting.
- Old `conveyors.lm`, `stroke_mm`, `mm_per_rev` and `cycle.belt_rpm` keys in `config.yaml` are ignored with a warning.

### Storage — two consumers, two writers

ACT (sequences) and CNN (single snapshots) have completely different needs. Mixing them
in one class explodes the conditionals, so they are split behind a `DatasetWriter`
interface (Strategy).

The CNN side keeps only **one representative set**, not the whole episode. Using every
sequence frame as classification data floods it with thousands of near-identical images,
causing overfitting and train/val leakage.

An aborted episode is deleted directory and all in `abort()`. A half-written episode is
the worst kind of data — it quietly erodes training quality.

---

## Data layout

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
├── background/{cam2..cam5}.jpg   <- empty-stage reference saved by 'capture background'
└── models/<object_id>.stl        <- CAD saved by 'register reference model'
```

`joint_states.parquet` columns:

| Column | Description |
|---|---|
| `frame_index` | Increments from 0 |
| `timestamp` | Seconds elapsed since episode start |
| `sync_skew_ms` | Inter-camera time skew for that frame set |
| `leader_joint_{0..4}` | Leader joint angles (deg) |
| `follower_joint_{0..4}` | Follower joint angles (deg) |
| `ts_{cam2..cam5}` | Per-camera capture time (monotonic) |

Adding `monotonic_offset` from `metadata.json` restores monotonic -> epoch. (monotonic is
used because the wall clock can run backwards under NTP correction, which is unsuitable
for frame ordering.)

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/meta` | Camera order, defect types, next episode ID |
| GET | `/api/status` | Combined recording / arm / teleop / camera status (main poll). Includes `sync_skew_ms` (inter-camera skew in ms, null with fewer than 2 cameras), `demo` (demo session state) and `cycle` (auto cycle state) |
| GET | `/api/ports` | Port + camera status snapshot |
| POST | `/api/ports/refresh` | Clear overrides and rescan |
| POST | `/api/ports/test` | Real open + ping verification |
| POST | `/api/ports/identify/{start,confirm}` | Unplug-diff 2-phase |
| GET | `/api/alerts[?refresh=1]` | Items a person should know about now (cause, impact and fix included; severity fault/warn/info). 5 s cache, `refresh=1` bypasses it |
| GET | `/api/diagnostics[?refresh=1]` | Environment, serial port, camera setting and safety-limit (joint limits, max relative target, homing speed `homing_speed_deg_per_s`, …) diagnostics. 30 s cache, `refresh=1` bypasses it (live connection state is `/api/ports`) |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG stream |
| GET | `/api/snapshot/<name>` | Single JPEG |
| POST | `/api/recording/{start,stop}` | Collection control (`start` and `stop` return 409 during a demo session, `start` during an auto cycle — the session thread owns the recording, and a manual stop would lose the episode. Emergency stop is unaffected) |
| POST | `/api/demo/start` | Start a demo recording session — body `object_id`, `task`, `episode_sec` (1–600), `reset_sec` (0–120). Teleop must be on, both arms connected, no recording or rollout running |
| POST | `/api/demo/{next,redo,end}` | Save and go next / discard and redo (while recording only) / save and end the session. Progress is `demo` in `/api/status`. Demo episodes are stored with `kind=demo` + `task` and create no classification images |
| GET | `/api/episodes` | List saved episodes (metadata.json) |
| PATCH | `/api/episodes/<id>` | Edit label / object ID (moves the classification folder too; `kind=demo` episodes leave classification folders alone) |
| DELETE | `/api/episodes/<id>` | Delete episode (also cleans classification images if last reference; demo episodes do not count as references) |
| GET | `/api/inspections?limit=` | List 3D inspection captures (newest first) with a label summary. `capture_id` is `YYYYmmdd_HHMMSS`; a capture in the same second gets `_02`, `_03` … |
| GET | `/api/inspections/<capture_id>/<cam>.jpg` | Original capture image |
| POST | `/api/inspections/<capture_id>/label` | Save a capture label — written only to `inspections/<id>/label.json` (`classification/` is never touched) |
| DELETE | `/api/inspections/<capture_id>` | Delete a capture (unlabeled only; 400 if labeled) |
| GET | `/api/objects` | Object list (from episodes, labeled captures and CAD models) with the recommended pose count |
| GET | `/api/readiness` | Data readiness (ACT episode count, good/defect object counts, goals) |
| POST | `/api/emergency_stop` | Emergency stop — auto cycle + teleop + all belts + recording + demo session at once |
| POST | `/api/emergency_reset` | Re-enable only the belt drivers the emergency stop switched off ('Enable drivers'; nothing moves). Connected belts only — a disconnected or released belt's port is not opened and is reported as `not_connected` |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,enable,servo,release}` | Conveyor (2 DC-motor belts) control (during an auto cycle, drive commands other than `stop` / `enable(false)` return 409). `speed` body is `{"duty": 0-100}` (PWM duty %; 400 out of range). The firmware refuses `dir` while running and `start` while the driver is off (400). State keys: `running`, `duty` (the set speed), `direction`, `enabled`, `servo_deg` (M1 only) |
| GET | `/api/cycle/status` | Auto cycle state — steps, progress message, verdict, basket counts and whether it can start (`ready`: blocking `reasons`, non-blocking `notes`). Same as the `cycle` key of `/api/status` |
| POST | `/api/cycle/start` | Start one cycle (pick → belt carry → inspect → sort). 400 + the reason if it is not ready or already running. After the check passes it opens the belt port before the arm moves; 400 if it cannot |
| POST | `/api/cycle/stop` | Abort at once — stops only the belt and the autorun (the gate and arm stay where they are). State becomes `aborted` |
| POST | `/api/cycle/baskets/empty` | Reset the count to 0 after emptying a basket — body `{"basket": "good"}`, `"defect"` or `"all"` |
| GET/POST | `/api/scan/model/<object_id>` | Query/upload CAD reference model (STL) |
| POST | `/api/scan/background` | Capture empty-stage background (for background subtraction; 409 during an auto cycle — the object on the belt would be baked into it) |
| POST | `/api/scan/capture` | Capture one set now that the conveyor is still and give a 3D verdict — body `settle_sec` (0–3), `resolution` (16–96). 400 if not a number, 409 during an auto cycle. Captures go to `inspections/<capture_id>/` |
| POST | `/api/teleop/{start,stop}` | Teleoperation (`start` returns 409 during an auto cycle) |
| POST | `/api/arm/home` | Homing (rejected while recording or during an auto cycle) |
| POST | `/api/train/{start,stop}` | ACT training control (demo/lerobot mode) |
| GET | `/api/train/status` | Training status + metric time series (poll) |
| GET | `/api/train/stream` | Training metrics SSE live stream |
| GET | `/api/train/runs/latest` | Summary of the latest run in `logs/train` (`run: null` if none; does not start following it) |
| GET | `/api/scan/objects` | List scannable objects that can be reconstructed |
| POST | `/api/scan/reconstruct` | 5 views -> 3D mesh (visual hull). The body is a stored object (`object_id`, `label`) or a stored inspection capture (`capture_id` — reads `inspections/<id>/<cam>.jpg` and rebuilds it the same way as `/api/scan/capture`). Compute-only, so it is allowed during an auto cycle |
| POST | `/api/scan/reconstruct_demo` | Synthetic reconstruction for an instant check without data |

Error responses all have the same shape:

```json
{ "ok": false, "error": { "code": "arm_port_not_found",
  "message": "Could not find the LEADER arm port…", "detail": { "role": "leader" } } }
```

---

## Real ACT training — LeRobot conversion

This repo's folder layout is meant for humans to read, so it is not a standard LeRobot
dataset. Convert it once before training.

```bash
python scripts/to_lerobot.py --repo-id omx/scan
python scripts/train_act.py --repo-id omx/scan --steps 20000
```

`scripts/train_act.py` is a wrapper that launches `lerobot-train` as a child process (it adds
`--policy.type=act`, the required `--policy.repo_id` and `--policy.push_to_hub=false` for you).
Training arguments the wrapper does not know go after `--`
(`... --repo-id omx/scan -- --policy.device=cuda --save_freq=5000`). `--log-freq` (default 50) is
passed to lerobot explicitly and is the point spacing of the dashboard curve. For every run the
wrapper writes, under `logs/train/<YYYYmmdd_HHMMSS>/`: `run.json` (arguments, status
RUNNING/DONE/FAILED, returncode, output_dir), `train.log` (raw lerobot output), `metrics.jsonl`
(parsed metrics), `gpu.jsonl` (GPU state, below) and `heartbeat` (an empty file whose modification
time is refreshed every 10 s — the server's evidence that the wrapper is alive, below), and it
echoes everything to the console. A run started in the same second gets a `_02`, `_03` … suffix on
its folder name (two digits, so name order = time order). To stop training, press Ctrl+C in the
terminal that launched the wrapper. The schema and the log parser live in
`backend/core/train_log.py`. Without lerobot the wrapper prints install instructions and fails
immediately.

While training runs, the wrapper samples `nvidia-smi` every 5 s into `gpu.jsonl` (as a child
process, without torch), and the LEROBOT-mode dashboard shows the recent summary (about 30 s) as a
'GPU' card. If `nvidia-smi` is missing it says so once and stops sampling right away; a transient
failure is retried on the next cycle, and 5 consecutive failures stop it. Either way training
keeps running.

The crux of the conversion is the observation/action mapping.

| LeRobot field | Our column | Meaning |
|---|---|---|
| `observation.state` | `follower_joint_*` | The pose the robot is in now |
| `action` | `leader_joint_*` | The pose the human commanded |
| `observation.images.<cam>` | `camera_<cam>/frame_*.jpg` | Frame at the same timestep |

If the two are swapped, the policy learns "current pose -> current pose": the loss drops
nicely but the robot does nothing. `tests/test_episodes.py` pins this mapping.

meta/*.jsonl, chunking and stats aggregation are not written by hand — they are left to
`LeRobotDataset.create/add_frame/save_episode`. Training needs lerobot anyway, and a
hand-written format silently drifts when the lerobot version bumps.

---

## ACT training metrics (live)

ACT is a **regression** model that predicts joint angles, so classification "accuracy"
does not apply directly. Three metrics quantify it instead.

| Metric | Meaning | Why it is needed |
|---|---|---|
| **L1 loss** | Mean absolute prediction error | Whether optimization is actually progressing |
| **Per-joint MAE (deg)** | Average degrees of error per joint | Diagnose which joint is not learning |
| **Success@τ** | Fraction of timesteps where every joint is within τ deg | Corresponds to classification "accuracy" — "is this close enough to grasp" |

Success@τ counts success only when **every joint is simultaneously** within the
threshold. If any single joint is far off, that motion is realistically a failure.

**DEMO mode** reproduces a real training curve (exponential loss decay + rising success)
without a GPU or dataset, so you can check the dashboard immediately. Even then the values
pass through the real `compute_metrics()`, so the chart logic is validated identically to
production. **LEROBOT mode** does not start training: it follows the latest `logs/train/*` run
left by `scripts/train_act.py` (see "Real ACT training" above) and draws the real loss curve on
the same chart (`LeRobotTrainer`, 0.5 s polling). There is no reason to run GPU training inside a
server that holds 4 cameras and the serial ports, so training stays outside the server and
survives a server restart. With no run on record it shows how to use the wrapper and ends as
FAILED.

What changes in LEROBOT mode:
- The x-axis is **STEP**, not EPOCH. The `step:1K` in lerobot logs is lossy (1000, 1200 and 1400
  all print `1K`), so the parsed value is not used; the step is restored as 'n-th metrics line ×
  log_freq'. A run resumed with `--resume` is offset in x by the checkpoint step.
- lerobot does not log val MAE or success rate, so those KPIs/charts show '—' or are hidden. VAL
  loss is drawn only where a `step N: eval_loss=…` line exists. An eval line for the same step
  attaches to that step's point (arriving twice still gives one point); a step without a metrics
  line becomes a separate point — the previous train loss is carried over, and an eval line that
  arrives before the first metrics line is kept with an empty train loss. So the same step never
  appears twice on the x-axis.
- **STOP only stops 'following'**; it does not kill the training process (the server is not the
  training's parent). If the wrapper is force-killed, `run.json` stays RUNNING, but once the
  heartbeat file has been still for more than 60 s the server treats the run as FAILED (an old
  run without a heartbeat is ended with STOP).

The live feed uses **SSE** (Server-Sent Events). Polling always lags by the interval, but
with SSE the chart grows the moment an epoch ends.

---

## Rollout — autonomous run with a trained ACT policy (new)

Once training finishes via the procedure above (`to_lerobot.py` -> `lerobot-train`), you
can drive the follower autonomously with that checkpoint. Training (`lerobot-train`)
deliberately runs outside this server process — it cannot hold 4 cameras and the serial
ports while occupying a GPU for hours (see `LeRobotTrainer` in
`backend/core/act_trainer.py`). Conversely, **rollout (inference) runs inside this
process** — `HardwareHub` already holds the follower/cameras. The relevant code is
`backend/core/lerobot_bridge.py` (`ActInferenceRunner`, inference only) and
`backend/core/rollout.py` (`AutoRunLoop`, symmetric with `TeleopLoop`) — even without
these two files (i.e. lerobot not installed) the rest of the dashboard still starts.

```bash
pip install -r requirements.txt -r requirements-lerobot.txt   # only for the rollout owner
```

```
POST /api/rollout/start   {"checkpoint": "outputs/train/<job>/checkpoints/<step>/pretrained_model"}
POST /api/rollout/start   {"waypoints": "lerobot_data/waypoints/episode_000001.json", "speed": 1.0}
POST /api/rollout/stop
GET  /api/rollout/status
```

**Waypoint playback (no training)** — if the task only needs the same motion repeated,
ACT training and a GPU are unnecessary. Extract a recorded episode into a list of angles
and replay it as-is:

```bash
python scripts/to_waypoints.py --episode 000001          # --tol adjusts sparsity (default 1.5 deg)
```

The output is JSON a human can read and fix in a text editor, and playback is
deterministic. `WaypointRunner` (`backend/core/waypoints.py`) has the same `predict()`
signature as `ActInferenceRunner`, so it reuses `AutoRunLoop` (watchdog / SafetyGuard /
stop path) as-is — there is no playback-only control loop. It does not look at the
cameras (`needs_frames = False`) and stops itself when it reaches the end of the
trajectory. `/api/rollout/status` gains `progress_sec` / `total_sec`. If the arm falls
more than 5 deg behind the target, the playback clock pauses (arrival gating) so a far
first waypoint does not cause the whole trajectory to be skipped.

- Either `checkpoint` or `waypoints` is required. For `checkpoint`, training is a
  separate process so this server has no way to know which checkpoint is latest — use the
  path printed on the last line of the `lerobot-train` log.
- teleop and rollout are both loops that call `follower.write()`, so they are **mutually
  exclusive**. If one is running, the other refuses to start. `SafetyGuard` (soft limit +
  relative-delta clamp) applies to rollout exactly as it does to teleop.
- The camera names passed as `frames` must match `CAMERA_ORDER` (`cam2`–`cam5`) — giving
  a camera name the policy did not see during training makes it silently ignore it or
  error.

### 4 problems actually hit on Windows (all reproduced and fixed)

While verifying this repo end to end on Windows with real hardware — teleop -> recording
-> `to_lerobot.py` conversion -> `lerobot-train` -> rollout — the following 4 issues came
up for real. They can recur when lerobot/torch versions change, so the diagnosis order is
kept here.

1. **torch DLL init failure** (`WinError 1114`, `c10.dll` load failure). If cv2/pandas
   load before torch, they claim a same-named runtime DLL (OpenMP family) first and break
   torch's DLL init. A no-op `import torch` (ignored on failure) is placed at the very top
   of `run.py` and `scripts/to_lerobot.py` to claim the slot — if you add a new entry
   point (script), add the same pattern.
   - If the symptom is this and `import torch` alone works but only breaks in a script,
     it is 100% this. Reproduce with `python -c "import cv2,pandas; import torch"`.
2. **Missing lerobot extras**: `pip install lerobot` alone leaves out both
   `lerobot.datasets` (needs `pip install 'lerobot[dataset]'`, which internally uses the
   HuggingFace `datasets` package) and `lerobot-train` (needs `pip install
   'lerobot[training]'`, which needs `accelerate`). `requirements-lerobot.txt` already
   accounts for this with `lerobot[dataset,training]`.
3. **`lerobot-train` errors immediately when `--policy.repo_id` is missing** (`ValueError:
   'repo_id' argument missing`). Even if you have no intention of pushing to the hub, this
   argument is required. Passing `--policy.push_to_hub=false` alongside it keeps it local.
4. **Rollout inference fails immediately from a joint-count mismatch** (`tensor a(6) must
   match tensor b(5)`). Even if the leader/follower physically have the same joint count,
   if `arms.*.joint_ids` in `config.yaml` differ in count (in our case the leader reads 5,
   the follower reads 6 including the gripper), the `observation.state` dimension of the
   dataset `to_lerobot.py` produced diverges from the actual `follower.read()` length.
   `ActInferenceRunner` reads
   `input_features["observation.state"]["shape"]` directly from the checkpoint's
   `config.json` and auto-trims to match, but to actually train pickup (gripper
   operation) you must include the gripper axis in `arms.leader.joint_ids` and re-collect
   — with the current config it only learns arm position and gripper open/close is not in
   the data at all.
5. **Calling `PolicyProcessorPipeline.from_pretrained()` directly breaks the
   postprocessor** (`EnvTransition must be a dictionary. Got Tensor`). This class's
   `to_transition` default is fixed to dictionary-only (`batch_to_transition`), so it is
   applied even to the postprocessor, which must accept an action (tensor). Always load
   via `lerobot.policies.make_pre_post_processors(policy_cfg=policy.config,
   pretrained_path=checkpoint_path)` (the same as the official `lerobot_eval.py`) — that
   function explicitly passes `to_transition=policy_action_to_transition` to the
   postprocessor.

> Item 4 above was resolved on 2026-08-11 by adding the gripper (ID 6) to
> `arms.leader.joint_ids` — now both leader and follower read 6 joints. However, the
> safety config (`safety.joint_limits`, `arms.follower.home_position`) still has only 5
> values, so the gripper axis can effectively move without limit — see "Next work" below.

### `feature/integration` merge notes (2026-08-14)

`feature/3axis-stepper-soft-pulse` (3-axis conveyor stepper integration, 1 STM32 BlackPill
board + 3x TB6600, soft-pulse switch) and `feature/lerobot-rollout` (this whole section —
the imitation learning pipeline) were merged into the `feature/integration` branch. **The
base is the 3axis side** — per the user's instruction, all "equipment wiring" values (port
numbers, camera matching, conveyor config) use the 3axis branch values as-is. The
imitation-learning code (`lerobot_bridge.py`, `rollout.py`, `act_trainer.py`,
`to_lerobot.py`, `requirements-lerobot.txt`) was taken as-is from lerobot-rollout — 3axis
did not touch this part, so no conflicts.

Conflicts were in only two places, `config.yaml` and `run.py`:
- `arms.leader/follower` in `config.yaml`: **port_hint keeps the 3axis values (COM6/COM7)**
  and the `serial_number` verified via `scripts/find_ports.py` on the lerobot-rollout side
  was added on top — serial_number has higher match priority than port_hint, so it is safe
  even if ports change and does not conflict with the port_hint policy.
- `run.py`: torch-first import (avoids the DLL ordering problem, lerobot-rollout) and the
  single-instance lock + stream port split (3axis) are separate concerns, so they were
  concatenated in the right order.

**Resolved automatically by the merge** (values the 3axis branch had already filled in):
- Gripper (6th) range in `safety.joint_limits` — opened generously to `[-180, 180]` with
  the current-based mode's current limit preventing over-tightening. **The angle itself
  was not measured**, so whether the gripper is actually safe must be checked with a
  slow/close observation on first run.
- Gripper (6th) value in `arms.follower.home_position` — filled with `0.0` on the 3axis
  side. **Whether this is a safe home angle for this unit's actual gripper is not
  verified** — have someone watch during the first homing on the integration branch.

**Not done yet (TODOs that remain regardless of the merge)**:
- [ ] **First verification of imitation learning with the gripper** — record 1–2 episodes
      (including gripper open/close; use a new `--repo-id` that does not overlap the
      existing `omx/scan` — mixing different joint counts breaks `to_lerobot.py`) ->
      convert -> `lerobot-train` -> check via `/api/rollout/start`. So far the full
      pipeline is only verified with 5-joint (no gripper) data.
- [ ] **Re-measure rollout `rate_hz`** — when requesting 5 Hz the observed tick count was
      higher than expected (likely a measurement error from tool-call latency; the code
      itself looks fine) — re-measure against a real wall clock.
- [ ] **Post-merge real integration test** — `pytest` (simulation) all passes, but running
      the 2-belt conveyor (replaced by DC motors) + imitation learning rollout in the same server
      process at once has not been checked on real hardware. `hardware.py` is the common
      contact point, so start there if something is off.
- [ ] **Full 3-way integration including CAD-based defect inspection (`cad.py`)** — conveyor
      and imitation learning were merged this time; CAD inspection (branch
      `feature/cad-inspection-and-fixes`, older than 3axis with its last push at 2026-08-11
      12:05) is not yet in this integration.

---

## 3D scan inspection

Build a 3D shape from the 5-view scan images and inspect it by rotating with the mouse.

**Why Visual Hull instead of photogrammetry.** The 4 views cam2–cam5 are widely spread
apart, so feature matching for an SfM pipeline like COLMAP almost always fails (it usually
needs tens to hundreds of images with large overlap). Instead, **silhouette-based space
carving** is used:

1. Extract the object silhouette (foreground mask) in each view,
2. project every voxel of a 3D voxel grid into all 5 views,
3. keep only voxels that are **inside the silhouette in every view** -> a convex
   approximation of the object.

Concave interiors (holes) cannot be recovered, but this is enough to confirm the shape
and mark defect locations — and above all it **always works with just 5 views**. The
voxels become a surface mesh via marching cubes (falls back to voxel cubes without
scikit-image).

**Inspection display.** The CNN verdict (good/defect + confidence) is shown as a KPI, and
defect areas are overlaid on the mesh surface with a **sunset-orange vertex color**. No
separate markers — they are visible right on the surface. The current defect locations are
demo values; in practice you pass the 3D back-projected locations of a CNN Grad-CAM or an
anomaly-detection heatmap as `defect_regions`.

**Viewer controls**: drag = rotate, scroll = zoom. Based on three.js r128; a minimal
orbit control is implemented directly instead of OrbitControls to reduce dependencies.

> **Check immediately without data**: press the **DEMO** button in the 3D SCAN INSPECTION
> section to see a synthetic shape + orange highlight without cameras or an object.

---

## Auto cycle — one button = pick → carry → inspect → sort

One press of [▶ Start cycle] in the top bar runs one cycle (flowchart 3.1 of the build report). A
server thread goes through the steps below; progress is polled from the `cycle` key of
`/api/status` (or `GET /api/cycle/status`). Implementation: `backend/core/cycle.py`.

1. **Pick** — With the gate at its closed angle and the belt direction/speed set, a waypoint replay
   (the same autorun as `/api/rollout/start`) picks the object and puts it on the belt. Fails if it
   does not finish within `pick_timeout_sec`, or on a fault. When the trajectory ends it waits
   another `pick_settle_sec` (default 1 s) before moving on to the carry step — so the belt does
   not move before the gripper has finished releasing the object (tune it on site).
2. **Carry** — Runs the belt for `to_scan_sec`, then stops it.
3. **Inspect** — Uses the same function as `/api/scan/capture` (`run_scan_capture`): check that the
   belt is still → capture → 3D verdict. The captures go to `inspections/<capture_id>/`. If
   capturing fails, the cycle fails too.
4. **Sort** — Good: run the belt for `good_run_sec`, then stop. Defect: run it for
   `defect_to_branch_sec`, stop, open the gate, wait `gate_hold_sec`, then close it. After that the
   matching basket count goes up by 1.

Verdict: `stats.quality.level == "low"` is a defect (an object that cannot be judged is never sent
to the good line); `anomaly.flagged_ratio >= defect_ratio` is a defect; anything else is good.

**Making the pick waypoints (once)**

1. In Equipment Control turn teleop on and record a "pick it up and put it on the belt"
   demonstration as one episode. Start from a rest pose and return to it — there is no homing
   between cycles, so the next cycle starts from the pose the last one ended in.
2. `python scripts/to_waypoints.py --episode <ID> --out lerobot_data/waypoints/pick_place.json`
3. Make `cycle.pick_waypoints` in `config.yaml` point at that file (the default is the path above).
   The other values in the same `cycle:` section — the wait after picking (`pick_settle_sec`,
   0–60 s), belt times and speed, gate angles, verdict threshold, basket capacity — are all starting
   points; every line is marked 'measure on site'. A bad value (out of range, not a number) is
   rejected as soon as the server starts.

**Safety rules**

- If the pre-start check (`ready`) trips on anything, the cycle does not start and gives the reasons
  (`reasons`): the pick motion file (paths inside the repo are shown relative to the repo), the cycle
  belt being in the config with a port set, its driver being enabled and the belt standing still,
  the follower being connected, at least 2 cameras for reconstruction, no teleop / rollout /
  recording / demo running, and basket room. Setting `pick_checkpoint` (ACT) refuses the start — it
  has no completion signal, so picking always ends in a timeout (use waypoints; the check blocks it
  rather than config validation, so the server still starts).
  - On real hardware the belt port is closed right after the server starts (it opens on the first
    command). `ready` neither opens the port nor blocks on it — it only adds a line to `notes`
    ('starting will open the belt port'). After the check passes, `POST /api/cycle/start` opens the
    port **before the arm moves**; if it cannot, it returns 400 with the reason and the arm does not
    move.
  - An emergency stop turns the belt driver off (DISABLE). Because the firmware refuses START while
    disabled, after an emergency stop a person must release it explicitly with 'Enable drivers' in
    Equipment Control (`POST /api/emergency_reset`) before a cycle can start. A driver that was
    switched off while the port was closed is checked after the start opens the port, and refused
    for the same reason.
- While a cycle runs, the server rejects with 409 (`cycle_busy`): starting teleop / rollout /
  recording / demo, manual captures (`/api/scan/capture`, `background`, `calibrate/shot`), belt drive
  commands (start, speed, dir, servo, release) and homing. A background captured
  mid-cycle would bake the object on the belt into the background and ruin every later verdict.
  Compute-only endpoints (`reconstruct`, `calibrate/solve`) are not blocked. Belt `stop`, emergency
  stop and `/api/cycle/stop` always work. **If a person stops any belt or releases its driver, the
  cycle stops too** (so the cycle never restarts a belt a person has just stopped). The two axes
  share the servo gate and the COM port, so manual operation during a cycle is blocked
  conservatively across the board, and stopping even another belt stops the cycle.
- On an abort or error only the belt and the autorun are stopped (belt first — stopping the autorun
  may have to wait for the hub lock). The gate and arm stay where they stopped (the gate returns to
  its closed angle when the next cycle starts). An emergency stop cuts the cycle first, waits
  briefly (up to 1 s) for a belt command that has already gone out, and only then stops and
  disables the belts — so a START that already passed its check cannot slip in after DISABLE and
  re-enable the driver. The autorun start, which moves the arm, re-checks abort / server shutdown
  inside the hub's own lock and does not start once a stop has finished. After a server shutdown has
  closed a belt port, it is not reopened. Every wait is an `Event.wait`, so a stop takes effect even
  mid-wait.
- When a basket is full (`basket_capacity`) the cycle does not start. After emptying it, pressing
  [Emptied] (`POST /api/cycle/baskets/empty`) sets the count back to 0. Counts live only in server
  memory, so they are 0 after a restart.
- **The first time, lower `pick_speed` and watch from next to the robot.**

---

## Known limitations

**Browser concurrent connection limit.** HTTP/1.1 is limited to 6 connections per host
(MDN). If 5 MJPEG streams take 5 of them, the polling API starves. Lowering the preview to
12 fps mitigates this, but if you need more, put an HTTP/2 reverse proxy in front or split
the streams onto a separate hostname.

**Flask dev server.** Streams occupy a worker per connection. `use_reloader=False` is
mandatory (the reloader spawns the process twice, opening the cameras twice and locking
the devices). For real use, use the gunicorn + gevent setup in `scripts/serve.sh`.

**Software synchronization.** There is no hardware trigger, so skew cannot be driven to
zero. If you need more precision you need an industrial camera that supports an external
trigger.

**USB bandwidth.** Putting 5 cameras on one host controller is not enough even for
640x480@30 without MJPG. Keep `fourcc: MJPG` and, if possible, spread the load over a
separate PCIe USB card.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| arm stays OFFLINE | Whether `dialout` is in `groups $USER`. If not, `sudo usermod -aG dialout $USER` then re-login |
| camera NO SIGNAL | Check the path with `v4l2-ctl --list-devices`. Whether another process holds it: `fuser /dev/video0` |
| fps is half the target | USB bandwidth shortage. Check `fourcc: MJPG`, spread across hubs |
| many skew warnings | Set `sample_rate_hz` lower than the camera fps (e.g. camera 30 -> sampling 20) |
| joint ID not responding | Check ID/baud rate with Dynamixel Wizard |

---

## Tests

```bash
pytest
```

35 tests run without hardware — safety clamp, path-traversal defense, state transitions,
sync regression, ACT regression metrics (success@τ boundary), 3D reconstruction, defect
overlay verification.

---

## License

MIT — see [LICENSE](LICENSE).
