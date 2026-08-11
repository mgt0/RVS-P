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

import logging
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

        # 전/후처리기(정규화)도 같은 체크포인트 폴더에 저장돼 있다. 없어도 동작은
        # 하지만(원본 값 그대로 씀) 학습 시 정규화했다면 결과가 어긋난다.
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
        """observation(팔로워 관절 + 카메라 프레임) → 다음 관절 목표(degree).

        frames의 키는 scripts/to_lerobot.py가 변환 시 쓴 것과 동일한 카메라
        이름(config.yaml의 CAMERA_ORDER, 예: cam1~cam5)이어야 한다 — 학습 때
        본 적 없는 카메라 이름을 주면 정책이 조용히 무시하거나 에러를 낸다.
        """
        torch = self._torch
        obs: dict = {
            "observation.state": torch.tensor([follower_joints], dtype=torch.float32, device=self._device),
        }
        for cam, image in frames.items():
            rgb = image[:, :, ::-1].copy()  # BGR(cv2) → RGB
            tensor = torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0) / 255.0
            obs[f"observation.images.{cam}"] = tensor.to(self._device)

        if self._preprocessor is not None:
            obs = self._preprocessor(obs)

        with torch.no_grad():
            action = self._policy.select_action(obs)

        if self._postprocessor is not None:
            action = self._postprocessor(action)

        return [float(v) for v in action.squeeze(0).detach().cpu().tolist()]
