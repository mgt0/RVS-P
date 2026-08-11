"""ACT 학습 오케스트레이션 + 실시간 메트릭 스트리밍.

설계
----
* 학습은 오래 걸리므로 별도 스레드에서 돌리고, 웹 요청은 즉시 반환한다.
* 학습 루프는 에폭마다 MetricHistory 에 기록한다. 프론트는 SSE(/api/train/stream)
  로 실시간 수신하거나 폴링(/api/train/status)으로 조회한다.
* Strategy: TrainerBackend 뒤에 두 구현.
    - LeRobotTrainer   : 실제 lerobot 패키지로 ACT 학습(설치 시)
    - DemoTrainer      : lerobot 없이도 UI/파이프라인을 검증하는 합성 학습 곡선.
                         수렴하는 loss 와 상승하는 success_rate 를 실제처럼 생성.

왜 데모 백엔드가 필요한가: 실제 학습은 GPU·데이터셋·수 시간이 필요하다.
대시보드의 실시간 차트가 제대로 그려지는지 먼저 확인하려면 가벼운 대역이 있어야 한다.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from .act_metrics import BatchMetrics, EpochRecord, MetricHistory, compute_metrics

logger = logging.getLogger(__name__)


class TrainerBackend(Protocol):
    def run(self, ctrl: "TrainingController") -> None: ...


class TrainingController:
    """학습 상태 + 중단 신호를 들고 있는 핸들.

    State: IDLE → RUNNING → (DONE | STOPPED | FAILED).
    학습 스레드는 should_stop 을 주기적으로 확인해 협조적으로 종료한다.
    강제 kill 은 GPU 메모리와 파일 핸들을 누수시키므로 쓰지 않는다.
    """

    def __init__(self, params: dict) -> None:
        self.params = params
        self.history = MetricHistory()
        self.state = "IDLE"
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()

    def set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self.state = state
            if error:
                self.error = error
            if state == "RUNNING" and self.started_at is None:
                self.started_at = time.time()
            if state in {"DONE", "STOPPED", "FAILED"}:
                self.finished_at = time.time()

    def status(self) -> dict:
        with self._lock:
            elapsed = None
            if self.started_at:
                end = self.finished_at or time.time()
                elapsed = round(end - self.started_at, 1)
            return {
                "state": self.state,
                "error": self.error,
                "elapsed_sec": elapsed,
                "params": self.params,
                **self.history.snapshot(),
            }


class DemoTrainer:
    """합성 학습 곡선 생성기.

    실제 학습의 전형적 형태(loss 지수 감소 + 노이즈, success 점진 상승)를
    재현해 대시보드 차트를 검증한다. 관절 수/에폭 수는 params 를 따른다.
    """

    def run(self, ctrl: TrainingController) -> None:
        p = ctrl.params
        epochs = int(p.get("epochs", 50))
        num_joints = int(p.get("num_joints", 5))
        chunk = int(p.get("chunk_size", 32))
        threshold = float(p.get("threshold_deg", 5.0))
        rng = np.random.default_rng(int(p.get("seed", 42)))

        ctrl.set_state("RUNNING")
        logger.info("DemoTrainer 시작: epochs=%d joints=%d", epochs, num_joints)

        # 관절마다 수렴 속도를 다르게 줘서 현실감을 준다(어떤 관절은 학습이 더 어렵다).
        joint_difficulty = rng.uniform(0.8, 1.6, size=num_joints)

        for epoch in range(1, epochs + 1):
            if ctrl.should_stop:
                ctrl.set_state("STOPPED")
                logger.info("학습 중단 요청 수신 (epoch %d)", epoch)
                return

            t0 = time.time()

            # 지수 감소 + 노이즈. epoch 가 커질수록 loss 가 바닥에 수렴.
            progress = epoch / epochs
            base = 12.0 * np.exp(-3.0 * progress)

            # 검증셋을 흉내 낸 예측/정답 생성 → 실제 compute_metrics 로 계산.
            # (차트가 그리는 값이 '진짜 메트릭 함수'를 통과하도록 하는 게 핵심)
            n = 256
            target = rng.uniform(-90, 90, size=(n, chunk, num_joints))
            noise_scale = base * joint_difficulty[None, None, :]
            pred = target + rng.normal(0, 1.0, target.shape) * noise_scale

            val: BatchMetrics = compute_metrics(
                pred, target, threshold_deg=threshold, loss_kind="l1"
            )
            train_loss = float(val.loss * rng.uniform(0.9, 1.05))  # train 은 살짝 낮게

            ctrl.history.append(
                EpochRecord(
                    epoch=epoch,
                    train_loss=round(train_loss, 4),
                    val_loss=round(val.loss, 4),
                    val_mae_deg=round(val.mae_overall_deg, 3),
                    val_success_rate=round(val.success_rate, 4),
                    val_mae_per_joint=val.mae_per_joint_deg,
                    lr=float(p.get("lr", 1e-4)) * (0.5 ** (epoch // max(epochs // 3, 1))),
                    seconds=round(time.time() - t0, 3),
                )
            )

            # 실제 학습처럼 에폭당 시간을 소비(차트가 실시간으로 자라는 걸 보이게).
            ctrl._stop_event.wait(float(p.get("epoch_delay_sec", 0.3)))

        ctrl.set_state("DONE")
        logger.info("DemoTrainer 완료")


class LeRobotTrainer:
    """실제 ACT 학습은 이 대시보드가 아니라 lerobot 의 CLI 가 담당한다.

    학습은 GPU 를 몇 시간 점유하므로, 카메라 5대와 시리얼 포트를 붙잡고 있는
    이 서버 프로세스 안에서 돌릴 일이 아니다. 별도 프로세스로 띄우면 서버를
    재시작해도 학습이 살아남고, 로그·체크포인트 관리도 lerobot 이 이미 한다.
    여기서는 다음에 실행할 두 줄을 알려주는 역할만 한다.
    """

    def run(self, ctrl: TrainingController) -> None:
        repo_id = ctrl.params.get("repo_id", "omx/scan")
        ctrl.set_state(
            "FAILED",
            "실제 ACT 학습은 별도 프로세스에서 실행합니다:\n"
            f"  1) python scripts/to_lerobot.py --repo-id {repo_id}\n"
            f"  2) lerobot-train --dataset.repo_id={repo_id} --policy.type=act\n"
            "이 화면의 실시간 차트는 DEMO 모드로 확인하세요.",
        )


class TrainingManager:
    """학습 세션 하나를 관리(동시 1개). 라우트가 쓰는 Facade."""

    def __init__(self, dataset_root: Path) -> None:
        self._dataset_root = dataset_root
        self._ctrl: TrainingController | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, params: dict, *, demo: bool = True) -> dict:
        with self._lock:
            if self._ctrl and self._ctrl.state == "RUNNING":
                raise RuntimeError("이미 학습이 진행 중입니다. 먼저 정지하세요.")

            params.setdefault("dataset_root", str(self._dataset_root))
            ctrl = TrainingController(params)
            backend: TrainerBackend = DemoTrainer() if demo else LeRobotTrainer()

            def _run():
                try:
                    backend.run(ctrl)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("학습 실패")
                    ctrl.set_state("FAILED", str(exc))

            thread = threading.Thread(target=_run, name="act-train", daemon=True)
            self._ctrl = ctrl
            self._thread = thread
            thread.start()
            return ctrl.status()

    def stop(self) -> dict:
        with self._lock:
            if not self._ctrl:
                raise RuntimeError("진행 중인 학습이 없습니다.")
            self._ctrl.request_stop()
            return self._ctrl.status()

    def status(self) -> dict:
        with self._lock:
            if not self._ctrl:
                return {"state": "IDLE", "records": [], "num_epochs": 0,
                        "best_val_loss": None, "best_epoch": None, "params": {}}
            return self._ctrl.status()
