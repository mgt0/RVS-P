"""lerobot 패키지 연동 다리 — rollout(자율 실행) 전용.

이 모듈에서만 `torch`/`lerobot`을 import한다(지연 임포트). 나머지 코드는
이 모듈이 없어도, 즉 lerobot 미설치 상태에서도 정상 기동해야 한다.
(arm_controller.py 가 dynamixel_sdk 를 connect() 시점에만 불러오는 것과 같은 이유.)

역할 분담 (origin/master 병합 이후 정리됨)
----------------------------------------
* 에피소드 → LeRobotDataset 변환: `scripts/to_lerobot.py` (학생이 이미 구현)
* 실제 ACT 학습: `lerobot-train` CLI를 별도 프로세스로 실행
  (이유는 `act_trainer.py`의 `LeRobotTrainer` 주석 참고 — 카메라·시리얼 포트를
  붙잡고 있는 이 서버 프로세스 안에서 GPU 학습을 몇 시간 돌릴 일이 아니다)
* **rollout(자율 실행)만 이 모듈이 담당한다** — 이건 반대로 이 프로세스 안에서
  돌아야 한다. 팔로워/카메라를 이미 HardwareHub가 쥐고 있어서, 학습된
  체크포인트로 매 tick 다음 관절 목표를 예측하는 추론은 여기서 하는 게 맞다.

버전 경고
--------
lerobot의 Python API는 버전마다 모듈 경로/시그니처가 바뀐다. 아래 코드는
2026-08 시점의 huggingface/lerobot 공식 문서를 기준으로 작성했고,
`_import_act_policy()`에서 신/구 모듈 경로를 순서대로 시도한다. 그래도 안 맞으면
`python -c "from lerobot.policies.act.modeling_act import ACTPolicy"` 로
실제 경로를 먼저 확인할 것.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class LeRobotUnavailableError(RuntimeError):
    """lerobot/torch 미설치 또는 예상한 모듈 구조를 못 찾았을 때."""


def _import_act_policy() -> Any:
    """lerobot 버전별로 ACTPolicy 모듈 경로가 다를 수 있어 후보를 순서대로 시도한다."""
    import importlib

    try:
        import torch  # noqa: F401
    except ImportError as exc:
        raise LeRobotUnavailableError(
            "torch가 설치되어 있지 않습니다. `pip install torch` 후 다시 시도하세요."
        ) from exc

    candidates = ["lerobot.policies.act.modeling_act", "lerobot.common.policies.act.modeling_act"]
    last_exc: Exception | None = None
    for mod_path in candidates:
        try:
            return importlib.import_module(mod_path).ACTPolicy
        except (ImportError, AttributeError) as exc:  # noqa: PERF203
            last_exc = exc
            continue

    raise LeRobotUnavailableError(
        "설치된 lerobot에서 ACTPolicy를 찾지 못했습니다. `pip show lerobot`으로 버전을 "
        "확인하고, lerobot_bridge.py의 후보 경로를 설치된 버전에 맞게 수정하세요. "
        f"(마지막 오류: {last_exc})"
    ) from last_exc


class ActInferenceRunner:
    """학습된 ACT 체크포인트로 매 tick 다음 관절 목표를 예측한다.

    checkpoint_path는 `lerobot-train`이 저장한 `pretrained_model` 디렉터리
    (policy.safetensors + config.json이 있는 곳)를 가리켜야 한다.
    보통 `outputs/train/<job_name>/checkpoints/<step>/pretrained_model/` 형태다
    — 정확한 경로는 학습 로그 마지막 줄에 출력된다.

    rollout.py 의 AutoRunLoop 가 TeleopLoop 의 `leader.read()` 자리에
    이 클래스의 `predict()` 를 넣는 구조 — 나머지(SafetyGuard 클램프,
    watchdog)는 TeleopLoop 와 완전히 동일하게 재사용한다.
    """

    def __init__(self, checkpoint_path: str, *, device_str: str = "auto") -> None:
        ACTPolicy = _import_act_policy()
        import torch

        self._torch = torch
        self._device = torch.device(
            ("cuda" if torch.cuda.is_available() else "cpu") if device_str == "auto" else device_str
        )

        try:
            self._policy = ACTPolicy.from_pretrained(checkpoint_path)
        except Exception as exc:  # noqa: BLE001
            raise LeRobotUnavailableError(
                f"체크포인트를 불러오지 못했습니다: {checkpoint_path}\n"
                "lerobot-train이 만든 'pretrained_model' 폴더(policy.safetensors + "
                f"config.json)를 정확히 가리키는지 확인하세요. (원본 오류: {exc})"
            ) from exc

        self._policy.to(self._device)
        self._policy.eval()

        # 학습 시 관측 차원을 config.json에서 직접 읽는다 — 리더(팔로워보다 관절
        # 수가 적을 수 있음, 그리퍼 등)와 팔로워의 관절 수가 다를 수 있고,
        # scripts/to_lerobot.py는 leader_joint_* 기준(action)으로 차원을 정하기
        # 때문에, 실제 팔로워에서 읽은 관절을 그대로 넘기면 개수가 안 맞을 수
        # 있다(실측: follower 6축 vs 학습된 state 5차원 → RuntimeError).
        # 여기서 잘라서 넘기고, 나머지 관절(그리퍼 등)은 그대로 유지된다
        # (ArmController.write()가 짧은 리스트를 앞쪽 관절에만 적용하기 때문).
        self._state_dim: int | None = None
        self._expected_cameras: set[str] | None = None
        try:
            cfg_path = Path(checkpoint_path) / "config.json"
            cfg_json = json.loads(cfg_path.read_text(encoding="utf-8"))
            input_features = cfg_json.get("input_features", {})
            state_feature = input_features.get("observation.state")
            if state_feature and state_feature.get("shape"):
                self._state_dim = int(state_feature["shape"][0])
            self._expected_cameras = {
                key.removeprefix("observation.images.")
                for key in input_features
                if key.startswith("observation.images.")
            } or None
            logger.info(
                "체크포인트 관측 스펙: state_dim=%s, cameras=%s",
                self._state_dim, self._expected_cameras,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "체크포인트 config.json에서 관측 스펙을 못 읽었습니다 — "
                "관절 수/카메라가 안 맞으면 여기서 원인을 먼저 의심할 것. 오류: %s", exc,
            )

        # 전/후처리기(정규화)도 같은 체크포인트 폴더에 저장돼 있다. 없어도 동작은
        # 하지만(원본 값 그대로 씀) 학습 시 정규화했다면 결과가 어긋난다.
        #
        # `PolicyProcessorPipeline.from_pretrained()`를 직접 두 번 부르면 실패한다 —
        # 그 클래스의 to_transition 기본값이 batch_to_transition(딕셔너리 전용)으로
        # 고정돼 있어서, 후처리기(액션=텐서를 받아야 함)에도 그게 그대로 적용돼
        # "EnvTransition must be a dictionary. Got Tensor"가 난다(실측으로 확인).
        # 공식 lerobot_eval.py가 쓰는 `make_pre_post_processors(pretrained_path=...)`
        # 는 후처리기에 to_transition=policy_action_to_transition을 명시적으로
        # 넣어줘서 이 문제가 없다 — 반드시 이 팩토리 함수를 통해서만 로드할 것.
        self._preprocessor = None
        self._postprocessor = None
        try:
            try:
                from lerobot.policies import make_pre_post_processors
            except ImportError:
                from lerobot.common.policies.factory import make_pre_post_processors

            self._preprocessor, self._postprocessor = make_pre_post_processors(
                policy_cfg=self._policy.config,
                pretrained_path=checkpoint_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "전/후처리기 로드 실패 — 정규화 없이 원본 값으로 동작합니다. 오류: %s", exc,
            )

        if hasattr(self._policy, "reset"):
            self._policy.reset()

        # 공식 추론 경로(lerobot/common/control_utils.py의 predict_action)가 쓰는
        # 헬퍼. 관측 dict → 배치 텐서 변환(permute/정규화/unsqueeze) + task/robot_type
        # 키 채우기까지 여기서 다 해준다. 손으로 텐서를 만들면 이 헬퍼가 채우는
        # 부가 키(예: task)가 빠져 전처리기 내부에서 "EnvTransition must be a
        # dictionary. Got Tensor" 같은 에러가 난다 — 실측으로 확인함. 반드시 이걸 쓸 것.
        try:
            from lerobot.policies import prepare_observation_for_inference
        except ImportError:
            from lerobot.common.policies.utils import prepare_observation_for_inference
        self._prepare_observation = prepare_observation_for_inference

    def predict(self, *, follower_joints: list[float], frames: dict[str, np.ndarray]) -> list[float]:
        """observation(팔로워 관절 + 카메라 프레임) → 다음 관절 목표(degree).

        frames의 키는 scripts/to_lerobot.py가 변환 시 쓴 것과 동일한 카메라
        이름(config.yaml의 CAMERA_ORDER, 예: cam1~cam5)이어야 한다 — 학습 때
        본 적 없는 카메라 이름을 주면 정책이 조용히 무시하거나 에러를 낸다.

        follower_joints는 팔로워가 실제로 갖고 있는 관절 수(그리퍼 포함 등)만큼
        넘어오지만, 학습된 state 차원(self._state_dim, action=leader_joint_* 기준)
        과 다를 수 있다 — 다르면 앞쪽 self._state_dim개만 잘라서 쓴다.
        """
        np_arr = np.asarray  # 지역 별칭
        state = follower_joints
        if self._state_dim is not None and len(state) != self._state_dim:
            if len(state) > self._state_dim:
                state = state[: self._state_dim]
            else:
                logger.warning(
                    "팔로워 관절 수(%d)가 학습된 state 차원(%d)보다 적습니다 — "
                    "그대로 넘깁니다(에러 가능성 있음).", len(state), self._state_dim,
                )

        # prepare_observation_for_inference는 순수 numpy 입력을 기대한다
        # (내부에서 torch.from_numpy + permute + /255 정규화 + batch dim + task/robot_type
        # 키 추가까지 전부 처리) — 여기서 텐서로 미리 바꾸면 안 된다.
        raw_obs: dict[str, np.ndarray] = {
            "observation.state": np_arr(state, dtype=np.float32),
        }
        cams = frames.items() if self._expected_cameras is None else (
            (k, v) for k, v in frames.items() if k in self._expected_cameras
        )
        for cam, image in cams:
            rgb = image[:, :, ::-1].copy()  # BGR(cv2) → RGB, (H, W, C) uint8 그대로
            raw_obs[f"observation.images.{cam}"] = rgb

        torch = self._torch
        with torch.no_grad():
            obs = self._prepare_observation(raw_obs, self._device, task="", robot_type=None)
            if self._preprocessor is not None:
                obs = self._preprocessor(obs)

            action = self._policy.select_action(obs)

            if self._postprocessor is not None:
                action = self._postprocessor(action)

        return [float(v) for v in action.squeeze(0).detach().cpu().tolist()]
