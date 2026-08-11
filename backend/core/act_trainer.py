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
    """실제 lerobot ACT 학습 어댑터.

    자체 포맷 에피소드(`<dataset_root>/episodes/episode_*`)를 최초 1회
    공식 LeRobotDataset 으로 변환한 뒤(`lerobot_bridge.convert_episodes_to_dataset`),
    ACTPolicy 를 학습한다. 진행 상황은 DemoTrainer 와 동일한
    MetricHistory/EpochRecord 인터페이스로 흘려보내므로 대시보드 차트
    (`ACT 학습·실시간`)는 코드 변경 없이 그대로 동작한다.

    lerobot 의 Python API 는 버전에 따라 시그니처가 자주 바뀐다. 실제 연동
    로직은 전부 `lerobot_bridge.py` 에 격리해뒀으니, 설치된 버전과 안 맞으면
    그 파일만 보면 된다.
    """

    def __init__(self, dataset_root: Path) -> None:
        self._episodes_root = dataset_root / "episodes"
        self._dataset_cache_root = dataset_root / "lerobot_datasets"
        self._checkpoint_root = dataset_root / "checkpoints"

    def run(self, ctrl: TrainingController) -> None:
        try:
            from . import lerobot_bridge as lb
        except ImportError as exc:  # 이 모듈 자체가 없을 정도로 손상된 설치
            ctrl.set_state("FAILED", f"lerobot_bridge 로드 실패: {exc}")
            return

        p = ctrl.params
        repo_id = p.get("repo_id", "local/omx_pick_and_place")
        epochs = int(p.get("epochs", 50))
        threshold = float(p.get("threshold_deg", 5.0))
        steps_per_epoch = int(p.get("steps_per_epoch", 50))
        batch_size = int(p.get("batch_size", 8))
        device_str = p.get("device", "auto")

        ctrl.set_state("RUNNING")
        try:
            dataset_dir = self._dataset_cache_root / repo_id.replace("/", "__")
            if not dataset_dir.exists() or p.get("force_reconvert"):
                logger.info("에피소드 → LeRobotDataset 변환 시작 (repo_id=%s)", repo_id)
                result = lb.convert_episodes_to_dataset(
                    self._episodes_root, dataset_dir, repo_id=repo_id,
                )
                logger.info(
                    "변환 완료: %d episodes, %d frames, %.1fs",
                    result.num_episodes, result.num_frames, result.seconds,
                )

            session = lb.build_act_training_session(dataset_dir, repo_id, device_str=device_str)
            policy = session["policy"]
            optimizer = session["optimizer"]
            preprocessor = session["preprocessor"]
            torch = session["torch"]

            dataloader = torch.utils.data.DataLoader(
                session["dataset"], batch_size=batch_size, shuffle=True, drop_last=True,
            )

            for epoch in range(1, epochs + 1):
                if ctrl.should_stop:
                    ctrl.set_state("STOPPED")
                    logger.info("LeRobot 학습 중단 요청 수신 (epoch %d)", epoch)
                    return

                t0 = time.time()
                losses: list[float] = []
                last_batch = None
                step = 0
                for batch in dataloader:
                    batch = preprocessor(batch)
                    loss, _ = policy.forward(batch)
                    loss.backward()
                    optimizer.step()
                    optimizer.zero_grad()
                    losses.append(float(loss.item()))
                    last_batch = batch
                    step += 1
                    if step >= steps_per_epoch:
                        break

                train_loss = sum(losses) / max(len(losses), 1)

                val = None
                if last_batch is not None:
                    policy.eval()
                    val = lb.quick_eval(
                        policy, last_batch, threshold_deg=threshold,
                        torch_mod=torch, compute_metrics_fn=compute_metrics,
                    )
                    policy.train()

                ctrl.history.append(
                    EpochRecord(
                        epoch=epoch,
                        train_loss=round(train_loss, 4),
                        val_loss=round(val.loss, 4) if val else None,
                        val_mae_deg=round(val.mae_overall_deg, 3) if val else None,
                        val_success_rate=round(val.success_rate, 4) if val else None,
                        val_mae_per_joint=val.mae_per_joint_deg if val else [],
                        lr=float(optimizer.param_groups[0]["lr"]),
                        seconds=round(time.time() - t0, 3),
                    )
                )
                logger.info(
                    "[LeRobot] epoch %d/%d train_loss=%.4f val_success=%s (%.1fs)",
                    epoch, epochs, train_loss,
                    f"{val.success_rate:.2%}" if val else "N/A",
                    time.time() - t0,
                )

            self._checkpoint_root.mkdir(parents=True, exist_ok=True)
            ckpt_dir = self._checkpoint_root / repo_id.replace("/", "__")
            policy.save_pretrained(ckpt_dir)
            session["preprocessor"].save_pretrained(ckpt_dir)
            session["postprocessor"].save_pretrained(ckpt_dir)
            logger.info("체크포인트 저장 완료: %s", ckpt_dir)

            ctrl.set_state("DONE")
        except lb.LeRobotUnavailableError as exc:
            ctrl.set_state("FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("LeRobot 학습 실패")
            ctrl.set_state("FAILED", f"학습 중 오류: {exc}")


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
            backend: TrainerBackend = DemoTrainer() if demo else LeRobotTrainer(self._dataset_root)

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
