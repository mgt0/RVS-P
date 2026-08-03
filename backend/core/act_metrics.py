"""ACT 학습 메트릭 정의.

왜 '정확도'를 그대로 못 쓰는가
------------------------------
ACT(Action Chunking Transformer)는 관절 각도를 예측하는 '회귀' 모델이다.
분류의 accuracy(맞다/틀리다)가 아니라 '얼마나 가까운가'를 재야 한다.
아래 세 지표로 수치화한다:

  1. loss        — 최적화가 실제로 진행되는지 (L1 또는 MSE)
  2. joint MAE   — 관절별 평균 절대 오차(도). "실제로 몇 도 틀리나"
  3. success@τ   — 예측이 임계값 τ(도) 이내인 비율. 분류의 accuracy 에 대응.
                   현장에서 "이 정도면 집을 수 있나"를 가장 직관적으로 보여준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np


@dataclass
class BatchMetrics:
    """한 배치(또는 검증 스텝)의 집계 결과."""

    loss: float
    mae_overall_deg: float
    mae_per_joint_deg: list[float]
    success_rate: float          # success@threshold, 0.0~1.0
    threshold_deg: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute_metrics(
    pred: np.ndarray,
    target: np.ndarray,
    *,
    threshold_deg: float = 5.0,
    loss_kind: str = "l1",
) -> BatchMetrics:
    """예측/정답 관절 각도로부터 메트릭을 계산.

    pred, target: shape (B, T, J) 또는 (B, J). 도(degree) 단위 가정.
      B=배치, T=action chunk 길이, J=관절 수.

    threshold_deg: 이 오차 이내면 '성공'으로 센다. 로봇팔 반복정밀도와
      그리퍼 여유를 고려해 5도를 기본값으로 둔다.
    """
    pred = np.asarray(pred, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)

    if pred.shape != target.shape:
        raise ValueError(f"shape 불일치: pred{pred.shape} vs target{target.shape}")

    abs_err = np.abs(pred - target)                 # (..., J)

    # loss
    if loss_kind == "l1":
        loss = float(abs_err.mean())
    elif loss_kind == "mse":
        loss = float(((pred - target) ** 2).mean())
    else:
        raise ValueError(f"알 수 없는 loss_kind: {loss_kind}")

    # 관절 축(J)만 남기고 나머지를 평균 → 관절별 MAE
    joint_axis = tuple(range(abs_err.ndim - 1))
    mae_per_joint = abs_err.mean(axis=joint_axis)    # (J,)
    mae_overall = float(mae_per_joint.mean())

    # success@τ: 모든 관절이 동시에 임계값 이내인 타임스텝의 비율.
    # 관절 하나라도 크게 틀리면 그 동작은 실패로 보는 게 현실적이다.
    within = (abs_err <= threshold_deg).all(axis=-1)  # (...,)
    success_rate = float(within.mean())

    return BatchMetrics(
        loss=loss,
        mae_overall_deg=mae_overall,
        mae_per_joint_deg=[round(v, 3) for v in mae_per_joint.tolist()],
        success_rate=success_rate,
        threshold_deg=threshold_deg,
    )


@dataclass
class EpochRecord:
    """에폭 1회의 학습/검증 요약. 프론트 차트가 이 리스트를 그린다."""

    epoch: int
    train_loss: float
    val_loss: float | None
    val_mae_deg: float | None
    val_success_rate: float | None
    val_mae_per_joint: list[float] = field(default_factory=list)
    lr: float | None = None
    seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class MetricHistory:
    """학습 전체의 메트릭 시계열을 보관하는 스레드 안전 저장소.

    학습 스레드가 append 하고, HTTP 폴링/SSE 가 snapshot 을 읽는다.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._records: list[EpochRecord] = []
        self._best_val_loss: float | None = None
        self._best_epoch: int | None = None

    def append(self, record: EpochRecord) -> None:
        with self._lock:
            self._records.append(record)
            if record.val_loss is not None:
                if self._best_val_loss is None or record.val_loss < self._best_val_loss:
                    self._best_val_loss = record.val_loss
                    self._best_epoch = record.epoch

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "records": [r.to_dict() for r in self._records],
                "best_val_loss": self._best_val_loss,
                "best_epoch": self._best_epoch,
                "num_epochs": len(self._records),
            }

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._best_val_loss = None
            self._best_epoch = None
