# OMX SCAN CONTROL

[한국어](README.md) | **English** | [中文](README.zh.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.12-blue.svg)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)
![last commit](https://img.shields.io/github/last-commit/mgt0/OMX-FACTORY-SYSTEM)
![repo size](https://img.shields.io/github/repo-size/mgt0/OMX-FACTORY-SYSTEM)
![issues](https://img.shields.io/github/issues/mgt0/OMX-FACTORY-SYSTEM)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

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

Capturing 5 cameras each on its own thread leaves their phases uncorrelated. Just
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
│       ├── camera_cam1/frame_000000.jpg …
│       ├── camera_cam2/ … camera_cam5/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{cam1..cam5}.jpg + labels.json
    └── defect/<object_id>/{cam1..cam5}.jpg + labels.json
├── background/{cam1..cam5}.jpg   <- empty-stage reference saved by 'capture background'
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
| `ts_{cam1..cam5}` | Per-camera capture time (monotonic) |

Adding `monotonic_offset` from `metadata.json` restores monotonic -> epoch. (monotonic is
used because the wall clock can run backwards under NTP correction, which is unsuitable
for frame ordering.)

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/meta` | Camera order, defect types, next episode ID |
| GET | `/api/status` | Combined recording / arm / teleop / camera status (main poll) |
| GET | `/api/ports` | Port + camera status snapshot |
| POST | `/api/ports/refresh` | Clear overrides and rescan |
| POST | `/api/ports/test` | Real open + ping verification |
| POST | `/api/ports/identify/{start,confirm}` | Unplug-diff 2-phase |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG stream |
| GET | `/api/snapshot/<name>` | Single JPEG |
| POST | `/api/recording/{start,stop}` | Collection control |
| GET | `/api/episodes` | List saved episodes (metadata.json) |
| PATCH | `/api/episodes/<id>` | Edit label / object ID (moves the classification folder too) |
| DELETE | `/api/episodes/<id>` | Delete episode (also cleans classification images if last reference) |
| POST | `/api/emergency_stop` | Emergency stop — teleop + all belts + recording at once |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,release}` | Conveyor (multi-belt) control |
| GET/POST | `/api/scan/model/<object_id>` | Query/upload CAD reference model (STL) |
| POST | `/api/scan/background` | Capture empty-stage background (for background subtraction) |
| POST | `/api/teleop/{start,stop}` | Teleoperation |
| POST | `/api/arm/home` | Homing (rejected while recording) |
| POST | `/api/train/{start,stop}` | ACT training control (demo/lerobot mode) |
| GET | `/api/train/status` | Training status + metric time series (poll) |
| GET | `/api/train/stream` | Training metrics SSE live stream |
| GET | `/api/scan/objects` | List scannable objects that can be reconstructed |
| POST | `/api/scan/reconstruct` | 5 views -> 3D mesh (visual hull) |
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
lerobot-train --dataset.repo_id=omx/scan --policy.type=act
```

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
production. Selecting **LEROBOT mode** points you to running real training as a separate
process — convert with `scripts/to_lerobot.py`, then use the `lerobot-train` CLI (see
"Real ACT training" above). There is no reason to run GPU training inside a server that
holds 5 cameras and the serial ports.

The live feed uses **SSE** (Server-Sent Events). Polling always lags by the interval, but
with SSE the chart grows the moment an epoch ends.

---

## Rollout — autonomous run with a trained ACT policy (new)

Once training finishes via the procedure above (`to_lerobot.py` -> `lerobot-train`), you
can drive the follower autonomously with that checkpoint. Training (`lerobot-train`)
deliberately runs outside this server process — it cannot hold 5 cameras and the serial
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
- The camera names passed as `frames` must match `CAMERA_ORDER` (`cam1`–`cam5`) — giving
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
      the 3-axis conveyor (including `lm`) + imitation learning rollout in the same server
      process at once has not been checked on real hardware. `hardware.py` is the common
      contact point, so start there if something is off.
- [ ] **Full 3-way integration including CAD-based defect inspection (`cad.py`)** — conveyor
      and imitation learning were merged this time; CAD inspection (branch
      `feature/cad-inspection-and-fixes`, older than 3axis with its last push at 2026-08-11
      12:05) is not yet in this integration.

---

## 3D scan inspection

Build a 3D shape from the 5-view scan images and inspect it by rotating with the mouse.

**Why Visual Hull instead of photogrammetry.** The 5 views cam1–cam5 are widely spread
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
