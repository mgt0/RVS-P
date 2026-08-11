"""lerobot 패키지 연동 다리 (신규).

이 모듈에서만 `torch`/`lerobot`을 import한다(지연 임포트). 나머지 코드는
이 모듈이 없어도, 즉 lerobot 미설치 상태에서도 정상 기동해야 한다.
(arm_controller.py 가 dynamixel_sdk 를 connect() 시점에만 불러오는 것과
같은 이유 — DEMO 모드/대시보드 자체는 lerobot 없이도 계속 돌아가야 한다.)

역할 두 가지
-----------
1) 우리 자체 포맷(`lerobot_data/episodes/episode_XXXXXX/...`)을
   공식 LeRobotDataset으로 변환한다 → `convert_episodes_to_dataset()`
2) 변환된 데이터셋으로 ACTPolicy를 학습/추론하는 얇은 래퍼를 제공한다
   → `build_act_training_session()`, `ActInferenceRunner`

버전 경고
--------
lerobot의 Python API는 버전마다 모듈 경로/시그니처가 바뀐다(학생이 남긴
act_trainer.py 주석에도 이미 적혀 있던 문제). 아래 코드는 2026-08 시점의
huggingface/lerobot 공식 튜토리얼(examples/tutorial/act/act_training_example.py)
을 기준으로 작성했고, `_import_lerobot_modules()`에서 신/구 모듈 경로를
순서대로 시도한다. 그래도 안 맞으면 다음으로 실제 시그니처를 먼저 확인할 것:

    python -c "from lerobot.datasets.lerobot_dataset import LeRobotDataset; help(LeRobotDataset.create)"
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class LeRobotUnavailableError(RuntimeError):
    """lerobot/torch 미설치 또는 예상한 모듈 구조를 못 찾았을 때."""


def _import_lerobot_modules() -> dict[str, Any]:
    """lerobot 버전별로 모듈 경로가 다를 수 있어 후보를 순서대로 시도한다."""
    import importlib

    try:
        import torch  # noqa: F401
    except ImportError as exc:
        raise LeRobotUnavailableError(
            "torch가 설치되어 있지 않습니다. `pip install torch` 후 다시 시도하세요."
        ) from exc

    # (dataset, policy, config, factory, feature_utils, types) 모듈 경로 후보.
    # 최신 버전을 먼저 시도하고, 실패하면 v0.4 이전 레이아웃(lerobot.common.*)으로 폴백.
    candidates = [
        (
            "lerobot.datasets.lerobot_dataset",
            "lerobot.policies.act.modeling_act",
            "lerobot.policies.act.configuration_act",
            "lerobot.policies.factory",
            "lerobot.utils.feature_utils",
            "lerobot.configs.types",
        ),
        (
            "lerobot.common.datasets.lerobot_dataset",
            "lerobot.common.policies.act.modeling_act",
            "lerobot.common.policies.act.configuration_act",
            "lerobot.common.policies.factory",
            "lerobot.common.datasets.utils",
            "lerobot.common.datasets.utils",
        ),
    ]

    last_exc: Exception | None = None
    for ds_mod, policy_mod, cfg_mod, factory_mod, feat_mod, types_mod in candidates:
        try:
            ds = importlib.import_module(ds_mod)
            policy = importlib.import_module(policy_mod)
            cfg = importlib.import_module(cfg_mod)
            factory = importlib.import_module(factory_mod)
            feat = importlib.import_module(feat_mod)
            types_ = importlib.import_module(types_mod)
            return {
                "LeRobotDataset": ds.LeRobotDataset,
                "LeRobotDatasetMetadata": ds.LeRobotDatasetMetadata,
                "ACTPolicy": policy.ACTPolicy,
                "ACTConfig": cfg.ACTConfig,
                "make_pre_post_processors": getattr(factory, "make_pre_post_processors", None),
                "dataset_to_policy_features": getattr(feat, "dataset_to_policy_features", None),
                "FeatureType": getattr(types_, "FeatureType", None),
            }
        except (ImportError, AttributeError) as exc:  # noqa: PERF203
            last_exc = exc
            continue

    raise LeRobotUnavailableError(
        "설치된 lerobot에서 예상한 모듈 구조를 찾지 못했습니다. "
        "`pip show lerobot`으로 버전을 확인하고, lerobot_bridge.py 상단의 "
        "import 경로 후보를 설치된 버전에 맞게 추가/수정하세요. "
        f"(마지막 오류: {last_exc})"
    ) from last_exc


# ══════════════════════════ ① 데이터셋 변환 ══════════════════════════


@dataclass
class ConversionResult:
    repo_id: str
    root: Path
    num_episodes: int
    num_frames: int
    seconds: float


def convert_episodes_to_dataset(
    episodes_root: Path,
    output_root: Path,
    *,
    repo_id: str = "local/omx_pick_and_place",
    robot_type: str = "omx_follower",
    task: str = "pick the 3D print and place it on the conveyor",
    only_labels: tuple[str, ...] = ("good",),
    use_videos: bool = True,
    image_hw: tuple[int, int] = (480, 640),
) -> ConversionResult:
    """`lerobot_data/episodes/episode_*` (자체 포맷) → 공식 LeRobotDataset.

    매핑 규칙
    --------
    action              = leader_joint_*   (그 순간 사람이 리더로 지시한 값)
    observation.state   = follower_joint_* (그 순간 팔로워가 실제로 있던 위치)
    observation.images.<cam> = 카메라별 프레임 (BGR→RGB 변환)

    only_labels: 기본은 'good'만 쓴다. ACT는 "정상 동작 시연"을 배우는
    모방학습이라, defect 라벨 에피소드(있다면 이상 상황 대응 시연 등)까지
    섞으면 파지 정책 자체가 오염된다. 픽앤플레이스 하나만 배우려면
    good 라벨 에피소드 = 정상 파지 시연이므로 그것만 쓰면 된다.
    """
    mods = _import_lerobot_modules()
    LeRobotDataset = mods["LeRobotDataset"]

    episode_dirs = sorted(p for p in episodes_root.glob("episode_*") if p.is_dir())
    if not episode_dirs:
        raise LeRobotUnavailableError(f"{episodes_root} 에 변환할 에피소드가 없습니다.")

    first_meta = json.loads((episode_dirs[0] / "metadata.json").read_text(encoding="utf-8"))
    cameras: list[str] = list(first_meta["cameras"])
    joint_names: list[str] = list(first_meta["joint_names"])
    fps = int(first_meta["fps"])
    num_joints = len(joint_names)
    h, w = image_hw

    features: dict[str, dict] = {
        "observation.state": {"dtype": "float32", "shape": (num_joints,), "names": list(joint_names)},
        "action": {"dtype": "float32", "shape": (num_joints,), "names": list(joint_names)},
    }
    for cam in cameras:
        features[f"observation.images.{cam}"] = {
            "dtype": "video" if use_videos else "image",
            "shape": (h, w, 3),
            "names": ["height", "width", "channel"],
        }

    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=fps,
        root=output_root,
        robot_type=robot_type,
        features=features,
        use_videos=use_videos,
    )

    import cv2

    t0 = time.time()
    num_frames_total = 0
    used_episodes = 0
    skipped = 0

    for ep_dir in episode_dirs:
        meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
        if meta.get("label") not in only_labels:
            skipped += 1
            continue

        df = pd.read_parquet(ep_dir / "joint_states.parquet")
        for _, row in df.iterrows():
            idx = int(row["frame_index"])
            frame: dict[str, np.ndarray] = {
                "observation.state": np.array(
                    [row[f"follower_joint_{j}"] for j in range(num_joints)], dtype=np.float32
                ),
                "action": np.array(
                    [row[f"leader_joint_{j}"] for j in range(num_joints)], dtype=np.float32
                ),
            }
            for cam in cameras:
                img_path = ep_dir / f"camera_{cam}" / f"frame_{idx:06d}.jpg"
                if not img_path.exists():
                    continue
                bgr = cv2.imread(str(img_path))
                if bgr is None:
                    continue
                frame[f"observation.images.{cam}"] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

            dataset.add_frame(frame, task=task)
            num_frames_total += 1

        dataset.save_episode()
        used_episodes += 1
        logger.info("변환 완료: %s (%d frames)", ep_dir.name, len(df))

    dataset.finalize()
    elapsed = time.time() - t0
    logger.info(
        "데이터셋 변환 완료: %s (%d/%d episodes 사용, label 불일치로 %d개 제외, %d frames, %.1fs)",
        output_root, used_episodes, len(episode_dirs), skipped, num_frames_total, elapsed,
    )

    if used_episodes == 0:
        raise LeRobotUnavailableError(
            f"label in {only_labels} 인 에피소드가 하나도 없습니다. "
            "녹화 시 label='good'으로 저장했는지 확인하세요."
        )

    return ConversionResult(
        repo_id=repo_id, root=output_root,
        num_episodes=used_episodes, num_frames=num_frames_total, seconds=round(elapsed, 1),
    )


# ══════════════════════════ ② 학습 세션 ══════════════════════════


def build_act_training_session(dataset_root: Path, repo_id: str, *, device_str: str = "auto") -> dict:
    """공식 튜토리얼(examples/tutorial/act/act_training_example.py)과
    동일한 순서로 ACTConfig/ACTPolicy/전처리기/옵티마이저/데이터셋을 만든다."""
    mods = _import_lerobot_modules()
    import torch

    LeRobotDataset = mods["LeRobotDataset"]
    LeRobotDatasetMetadata = mods["LeRobotDatasetMetadata"]
    ACTConfig = mods["ACTConfig"]
    ACTPolicy = mods["ACTPolicy"]
    make_pre_post_processors = mods["make_pre_post_processors"]
    dataset_to_policy_features = mods["dataset_to_policy_features"]
    FeatureType = mods["FeatureType"]

    device = torch.device(
        ("cuda" if torch.cuda.is_available() else "cpu") if device_str == "auto" else device_str
    )

    dataset_metadata = LeRobotDatasetMetadata(repo_id, root=dataset_root)
    features = dataset_to_policy_features(dataset_metadata.features)
    output_features = {k: ft for k, ft in features.items() if ft.type is FeatureType.ACTION}
    input_features = {k: ft for k, ft in features.items() if k not in output_features}

    cfg = ACTConfig(input_features=input_features, output_features=output_features)
    policy = ACTPolicy(cfg)
    policy.train()
    policy.to(device)

    preprocessor, postprocessor = make_pre_post_processors(cfg, dataset_stats=dataset_metadata.stats)

    delta_timestamps = {"action": [i / dataset_metadata.fps for i in cfg.action_delta_indices]}
    delta_timestamps |= {
        k: [i / dataset_metadata.fps for i in cfg.observation_delta_indices] for k in cfg.image_features
    }
    dataset = LeRobotDataset(repo_id, root=dataset_root, delta_timestamps=delta_timestamps)

    optimizer = cfg.get_optimizer_preset().build(policy.parameters())

    return {
        "device": device, "policy": policy, "preprocessor": preprocessor,
        "postprocessor": postprocessor, "dataset": dataset, "optimizer": optimizer, "torch": torch,
    }


def quick_eval(policy, batch, *, threshold_deg: float, torch_mod, compute_metrics_fn, max_samples: int = 8):
    """배치 하나로 대략적인 검증 지표를 낸다 (엄밀한 held-out val split이 아님).

    ACTPolicy 내부에서 예측 텐서를 직접 꺼내는 방법이 버전마다 달라 select_action()을
    표본 몇 개에 대해 반복 호출하는 방식으로 우회한다. 그래도 실패하면 None을 반환하고,
    호출부(act_trainer.py)는 val_* 필드를 None으로 채운 채 학습을 계속 진행한다.
    """
    try:
        with torch_mod.no_grad():
            target = batch["action"].detach().cpu().numpy()
            batch_size = target.shape[0]
            preds = []
            for i in range(min(batch_size, max_samples)):
                obs_i = {k: v[i : i + 1] for k, v in batch.items() if k.startswith("observation")}
                a = policy.select_action(obs_i)
                preds.append(np.asarray(a.detach().cpu()))
            pred = np.stack(preds)[:, None, :]           # (n, 1, J) — 첫 스텝만 비교
            tgt = target[: len(preds), :1, :]
            return compute_metrics_fn(pred, tgt, threshold_deg=threshold_deg, loss_kind="l1")
    except Exception as exc:  # noqa: BLE001
        logger.warning("quick_eval 실패 — val 지표는 이번 에폭에 None으로 기록합니다: %s", exc)
        return None


# ══════════════════════════ ③ 추론(rollout) ══════════════════════════


class ActInferenceRunner:
    """학습된 ACT 체크포인트로 매 tick 다음 관절 목표를 예측한다.

    rollout.py 의 AutoRunLoop 가 TeleopLoop 의 `leader.read()` 자리에
    이 클래스의 `predict()` 를 넣는 구조 — 나머지(SafetyGuard 클램프,
    watchdog)는 TeleopLoop 와 완전히 동일하게 재사용한다.
    """

    def __init__(self, checkpoint_path: str, *, device_str: str = "auto") -> None:
        mods = _import_lerobot_modules()
        import torch

        self._torch = torch
        ACTPolicy = mods["ACTPolicy"]

        self._device = torch.device(
            ("cuda" if torch.cuda.is_available() else "cpu") if device_str == "auto" else device_str
        )

        self._policy = ACTPolicy.from_pretrained(checkpoint_path)
        self._policy.to(self._device)
        self._policy.eval()

        self._preprocessor = None
        self._postprocessor = None
        try:
            from lerobot.processor import PolicyProcessorPipeline

            self._preprocessor = PolicyProcessorPipeline.from_pretrained(
                checkpoint_path, config_filename="preprocessor_config.json"
            )
            self._postprocessor = PolicyProcessorPipeline.from_pretrained(
                checkpoint_path, config_filename="postprocessor_config.json"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "전/후처리기 로드 실패 — 정규화 없이 원본 값으로 동작합니다 "
                "(체크포인트 폴더에 preprocessor_config.json / postprocessor_config.json 확인). "
                "오류: %s", exc,
            )

        if hasattr(self._policy, "reset"):
            self._policy.reset()

    def predict(self, *, follower_joints: list[float], frames: dict[str, np.ndarray]) -> list[float]:
        """observation(팔로워 관절 + 카메라 프레임) → 다음 관절 목표(degree)."""
        torch = self._torch
        obs: dict = {
            "observation.state": torch.tensor([follower_joints], dtype=torch.float32, device=self._device),
        }
        for cam, image in frames.items():
            rgb = image[:, :, ::-1].copy()  # BGR → RGB
            tensor = torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0) / 255.0
            obs[f"observation.images.{cam}"] = tensor.to(self._device)

        if self._preprocessor is not None:
            obs = self._preprocessor(obs)

        with torch.no_grad():
            action = self._policy.select_action(obs)

        if self._postprocessor is not None:
            action = self._postprocessor(action)

        return [float(v) for v in action.squeeze(0).detach().cpu().tolist()]
