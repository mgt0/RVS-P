"""ACT 학습 오케스트레이션 + 실시간 메트릭 스트리밍.

설계
----
* 학습은 오래 걸리므로 별도 스레드에서 돌리고, 웹 요청은 즉시 반환한다.
* 학습 루프는 에폭마다 MetricHistory 에 기록한다. 프론트는 SSE(/api/train/stream)
  로 실시간 수신하거나 폴링(/api/train/status)으로 조회한다.
* Strategy: TrainerBackend 뒤에 두 구현.
    - LeRobotTrainer   : 서버 밖 CLI 학습(scripts/train_act.py)이 남긴 로그를 따라 읽어
                         실제 loss 곡선을 그린다(학습 자체는 이 프로세스에서 돌리지 않는다)
    - DemoTrainer      : lerobot 없이도 UI/파이프라인을 검증하는 합성 학습 곡선.
                         수렴하는 loss 와 상승하는 success_rate 를 실제처럼 생성.

왜 데모 백엔드가 필요한가: 실제 학습은 GPU·데이터셋·수 시간이 필요하다.
대시보드의 실시간 차트가 제대로 그려지는지 먼저 확인하려면 가벼운 대역이 있어야 한다.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from .act_metrics import BatchMetrics, EpochRecord, MetricHistory, compute_metrics
from .train_log import (
    LOG_FILE,
    METRICS_FILE,
    TRAIN_LOG_ROOT,
    latest_run_dir,
    read_gpu,
    read_new_records,
    read_run,
)

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
        # LEROBOT 전용: 래퍼가 남긴 gpu.jsonl 의 최근 요약. DEMO 는 계속 None.
        self.gpu: dict | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()

    def set_gpu(self, gpu: dict | None) -> None:
        # 새 dict 를 통째로 바꿔 끼우고 제자리에서 고치지 않는다 — status() 가 참조 그대로 내보낸다.
        with self._lock:
            self.gpu = gpu

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
                # 학습이 도는 동안만 내보낸다. 끝난 뒤에는 따라 읽기가 멈춰 마지막 값이 낡은 채 굳는다.
                "gpu": self.gpu if self.state == "RUNNING" else None,
                **self.history.snapshot(),
            }


# DEMO 가 내보내는 합성 미터. 학습 화면 초안(Train.dc.html)의 생성식을 옮겼다. 1 에폭을 100 스텝으로
# 보고, lerobot 콘솔과 같은 키의 값을 만들어 '실제 학습 로그' 화면의 지표 패널을 DEMO 로도 확인하게 한다.
_DEMO_BATCH = 8
_DEMO_EP_FRAMES = 600            # 에피소드 평균 프레임 수
_DEMO_EPISODES = 14
_DEMO_STEPS_PER_EPOCH = 100
_DEMO_KL_WEIGHT = 10             # lerobot ACT: loss = l1_loss + kl_weight × kld_loss
_DEMO_OVERHEAD_S = 0.004         # smp/s = 배치 ÷ (updt_s + data_s + overhead)


def _wobble(i: float, a: float, b: float) -> float:
    return 0.6 * math.sin(i * a) + 0.4 * math.sin(i * b + 1)


def _demo_meters(epoch: int, epochs: int, train_loss: float) -> dict:
    """에폭 하나의 합성 미터. updt_s·data_s 는 마지막 에폭이 정확히 0.215·0.031 이 되게 잡음을 맞춘다."""
    step = epoch * _DEMO_STEPS_PER_EPOCH
    kld = (0.0036 + 0.026 * math.exp(-step / 1500)
           + 0.0004 * _wobble(epoch, 2.3, 4.1) * math.exp(-step / 4000))
    upd = 0.215 + 0.006 * (_wobble(epoch, 2.1, 4.7) - _wobble(epochs, 2.1, 4.7))
    dat = 0.031 + 0.004 * (_wobble(epoch, 3.3, 5.9) - _wobble(epochs, 3.3, 5.9))
    smpl = step * _DEMO_BATCH
    return {
        "smpl": smpl,
        "ep": round(smpl / _DEMO_EP_FRAMES, 1),
        "epch": round(smpl / (_DEMO_EPISODES * _DEMO_EP_FRAMES), 2),
        "grdn": round(12.56 + 80 * math.exp(-step / 900)
                      + (1 + 5 * math.exp(-step / 1800)) * _wobble(epoch, 2.9, 5.3), 1),
        "data_s": round(dat, 3),
        "updt_s": round(upd, 3),
        "smp/s": round(_DEMO_BATCH / (upd + dat + _DEMO_OVERHEAD_S), 1),
        "mem_gb": round(5.4 - 0.6 * math.exp(-epoch / 5), 1),
        "l1_loss": round(max(train_loss - _DEMO_KL_WEIGHT * kld, 0.0), 4),
        "kld_loss": round(kld, 4),
    }


class DemoTrainer:
    """합성 학습 곡선 생성기.

    실제 학습의 전형적 형태(loss 지수 감소 + 노이즈, success 점진 상승)를
    재현해 대시보드 차트를 검증한다. 관절 수/에폭 수는 params 를 따른다.
    """

    def run(self, ctrl: TrainingController) -> None:
        # 합성 미터가 이 배치로 계산됐다는 걸 알린다(화면이 '스텝 × 배치'를 적는 근거).
        # LeRobotTrainer.run 과 같은 이유로 제자리에서 고치지 않고 새 dict 로 바꾼다.
        ctrl.params = {**ctrl.params, "batch_size": _DEMO_BATCH}
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
                    meters=_demo_meters(epoch, epochs, train_loss),
                )
            )

            # 실제 학습처럼 에폭당 시간을 소비(차트가 실시간으로 자라는 걸 보이게).
            ctrl._stop_event.wait(float(p.get("epoch_delay_sec", 0.3)))

        ctrl.set_state("DONE")
        logger.info("DemoTrainer 완료")


class LeRobotTrainer:
    """실제 ACT 학습의 '로그 따라 읽기'. 학습 자체는 이 프로세스 밖에서 돈다.

    학습은 GPU 를 몇 시간 점유하므로, 카메라 4대와 시리얼 포트를 붙잡고 있는
    이 서버 프로세스 안에서 돌릴 일이 아니다. 별도 프로세스로 띄우면 서버를
    재시작해도 학습이 살아남고, 로그·체크포인트 관리도 lerobot 이 이미 한다.
    그래서 학습은 래퍼(scripts/train_act.py → lerobot-train)가 돌리며
    logs/train/<시각>/ 에 기록을 남기고, 여기서는 가장 최근 실행의 metrics.jsonl 을
    증분으로 읽어 DemoTrainer 와 같은 history 에 넣는다.

    * 이미 끝난 실행에 붙으면 전체 곡선을 한 번에 채우고 DONE/FAILED 로 끝난다.
    * STOP 은 '따라 읽기' 중단일 뿐 학습 프로세스를 죽이지 않는다. 서버는 학습의
      부모가 아니어서 죽일 수도 없고, 화면 버튼 한 번에 몇 시간짜리 학습이 날아가면
      안 된다. 학습을 멈추려면 래퍼를 띄운 터미널에서 Ctrl+C 한다.
    * 래퍼가 강제 종료되면 run.json 은 RUNNING 으로 남지만, 래퍼가 10초마다 갱신하는 heartbeat 파일이
      60초 넘게 낡으면 read_run 이 status 를 FAILED("stale": True)로 바꿔 돌려준다(프로세스 생존은 보지
      않는다). heartbeat 가 없는 옛 실행은 여전히 RUNNING 으로 남으니 STOP 으로 따라 읽기를 끊는다.
    * epoch 자리에는 복원한 학습 스텝이 들어간다(train_log.TrainLogReader). lerobot 로그에는
      val MAE·success 가 없어 해당 필드는 None 이고, val_loss 는 평가 줄이 있을 때만 채운다.
    * 같은 폴더의 gpu.jsonl(래퍼가 nvidia-smi 로 남김) 최근 요약을 매 폴링마다 ctrl.gpu 에 싣는다.
      status()/SSE 스냅샷의 `gpu` 로 나가 대시보드 'GPU' 카드가 된다. 파일이 없거나 낡으면 None.

    (참고) rollout(자율 실행)은 반대로 이 프로세스 안에서 돌아야 한다 —
    팔로워/카메라를 이미 이 프로세스가 쥐고 있기 때문이다. 그건
    `backend/core/rollout.py`의 `AutoRunLoop`가 담당하고, 학습된 체크포인트만
    (`scripts/to_lerobot.py` → `scripts/train_act.py`가 만든 것) 넘겨받아 추론만 한다.
    """

    POLL_SEC = 0.5
    # metrics.jsonl 의 train 줄에서 EpochRecord 의 이름 있는 필드로 가는 키 — 나머지가 meters 다.
    _NOT_METERS = frozenset({"kind", "step", "loss", "lr"})

    def __init__(self, log_root: Path = TRAIN_LOG_ROOT) -> None:
        self._log_root = log_root

    def run(self, ctrl: TrainingController) -> None:
        run_dir = latest_run_dir(self._log_root)
        if run_dir is None:
            repo_id = ctrl.params.get("repo_id", "omx/scan")
            ctrl.set_state(
                "FAILED",
                "학습 실행 기록이 없습니다. 실제 ACT 학습은 별도 프로세스에서 먼저 실행하세요:\n"
                f"  1) python scripts/to_lerobot.py --repo-id {repo_id}\n"
                f"  2) python scripts/train_act.py --repo-id {repo_id} --steps 20000\n"
                f"그 실행이 {self._log_root} 에 남기는 로그를 이 화면이 따라 읽어 loss 곡선을 그립니다.\n"
                "합성 곡선으로 화면만 확인하려면 DEMO 모드를 쓰세요.\n"
                "학습이 끝나면 체크포인트 경로로 POST /api/rollout/start 를 호출하세요.",
            )
            return

        meta = read_run(run_dir) or {}
        # status() 가 params 를 참조 그대로 내보내므로 제자리에서 고치지 않고 새 dict 로 바꾼다
        # (다른 스레드가 직렬화하는 중에 키가 늘면 RuntimeError).
        ctrl.params = {
            **ctrl.params,
            "run_id": run_dir.name,
            "total_steps": meta.get("steps"),
            "log_freq": meta.get("log_freq"),
            # 래퍼에 --batch-size 를 안 줬으면 None(lerobot 기본값을 서버는 모른다) — 화면이 ETA 를 다른 식으로 낸다.
            "batch_size": (meta.get("args") or {}).get("batch_size"),
        }
        ctrl.set_state("RUNNING")
        logger.info("학습 로그 따라 읽기 시작: %s", run_dir)

        pos = 0
        last: EpochRecord | None = None
        while True:
            # 상태를 먼저 읽고 지표를 나중에 읽는다. 래퍼는 마지막 지표를 쓴 '뒤에' DONE 을
            # 기록하므로, 종료를 본 시점의 지표 읽기는 마지막 줄까지 반드시 포함한다.
            meta = read_run(run_dir) or meta
            records, pos = read_new_records(run_dir / METRICS_FILE, pos)
            for rec in records:
                last = self._apply(ctrl, rec, last)
            ctrl.set_gpu(read_gpu(run_dir))

            status = meta.get("status")
            if status == "DONE":
                ctrl.set_state("DONE")
                return
            if status == "FAILED":
                ctrl.set_state(
                    "FAILED",
                    "학습 래퍼의 heartbeat 가 멈췄습니다. 콘솔 창을 닫았거나 강제 종료된 것으로 보입니다. "
                    f"마지막 기록은 {run_dir / LOG_FILE} 에 있습니다."
                    if meta.get("stale") else
                    f"학습 프로세스가 실패로 끝났습니다 (returncode={meta.get('returncode')}). "
                    f"원인은 {run_dir / LOG_FILE} 끝부분에서 확인하세요.",
                )
                return
            if ctrl._stop_event.wait(self.POLL_SEC):
                ctrl.set_state(
                    "STOPPED",
                    "로그 따라 읽기를 멈췄습니다. 학습 프로세스는 계속 실행 중입니다 "
                    "(학습을 멈추려면 래퍼를 띄운 터미널에서 Ctrl+C).",
                )
                return

    @staticmethod
    def _apply(ctrl: TrainingController, rec: dict, last: EpochRecord | None) -> EpochRecord | None:
        """metrics.jsonl 기록 하나를 history 에 반영하고, 평가 손실을 붙일 직전 기록을 돌려준다.

        같은 스텝은 한 기록이다. 따로 두면 차트 x축에 같은 스텝이 두 번 나온다.
        """
        try:
            if rec.get("kind") == "train":
                new = EpochRecord(
                    epoch=int(rec["step"]),
                    train_loss=float(rec["loss"]),   # loss 가 nan 이라 빠진 줄은 KeyError 로 건너뛴다
                    val_loss=None,
                    val_mae_deg=None,
                    val_success_rate=None,
                    lr=rec.get("lr"),
                    # 위 넷 말고 숫자인 키는 전부 미터다. lerobot 버전마다 있는 키가 달라 이름을 가정하지 않는다.
                    meters={k: v for k, v in rec.items()
                            if k not in LeRobotTrainer._NOT_METERS and isinstance(v, (int, float))},
                )
                if last is not None and last.epoch == new.epoch:
                    # 같은 스텝의 평가 줄이 먼저 와서 만든 기록 — 지표 줄 값을 채우고 평가 손실은 그대로 둔다.
                    last.train_loss, last.lr, last.meters = new.train_loss, new.lr, new.meters
                else:
                    last = new
                    ctrl.history.append(last)
            elif rec.get("kind") == "eval":
                step, val = int(rec["step"]), float(rec["eval_loss"])
                if last is not None and last.epoch == step:
                    ctrl.history.set_val_loss(last, val)    # 같은 스텝이면 평가 줄이 또 와도 한 기록
                else:
                    # eval_freq 가 log_freq 의 배수가 아니거나 첫 지표 줄보다 먼저 오면 같은 스텝의 지표 줄이 없다.
                    # 직전 train loss 를 이어 붙여 별도 점으로 둔다(직전이 없으면 비워 둔다 — 차트는 빈 값을 건너뛴다).
                    last = EpochRecord(
                        epoch=step, train_loss=last.train_loss if last else None, val_loss=val,
                        val_mae_deg=None, val_success_rate=None, lr=last.lr if last else None,
                    )
                    ctrl.history.append(last)
        except (KeyError, TypeError, ValueError):
            logger.warning("해석할 수 없는 학습 기록을 건너뜀: %r", rec)
        return last


class TrainingManager:
    """학습 세션 하나를 관리(동시 1개). 라우트가 쓰는 Facade."""

    def __init__(self, dataset_root: Path, log_root: Path = TRAIN_LOG_ROOT) -> None:
        self._dataset_root = dataset_root
        self._log_root = log_root
        self._ctrl: TrainingController | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, params: dict, *, demo: bool = True) -> dict:
        with self._lock:
            if self._ctrl and self._ctrl.state == "RUNNING":
                raise RuntimeError("이미 학습이 진행 중입니다. 먼저 정지하세요.")

            params.setdefault("dataset_root", str(self._dataset_root))
            # 프론트가 EPOCH/STEP 라벨과 지표 유무를 서버 데이터로 정하도록 모드를 싣는다.
            # (토글 값에 맡기면 새로고침 뒤나 모드를 바꾼 직후에 라벨이 데이터와 어긋난다.)
            params["mode"] = "demo" if demo else "lerobot"
            ctrl = TrainingController(params)
            backend: TrainerBackend = DemoTrainer() if demo else LeRobotTrainer(self._log_root)

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

    def latest_run(self) -> dict | None:
        """가장 최근 실행(logs/train)의 요약. 따라 읽기를 시작하지 않는다 — 화면이 진입할 때 자동으로 붙을지 정하는 근거다."""
        run_dir = latest_run_dir(self._log_root)
        meta = read_run(run_dir) if run_dir else None
        if run_dir is None or meta is None:     # run.json 을 래퍼가 쓰는 중이면 읽기 실패 → 없는 것으로 본다
            return None
        return {
            "run_id": run_dir.name,
            "status": meta.get("status"),
            "steps": meta.get("steps"),
            "log_freq": meta.get("log_freq"),
            "started_at": meta.get("started_at"),
        }
