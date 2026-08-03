"""데이터셋 저장 계층.

요구된 폴더 구조를 그대로 만든다:

    lerobot_data/
    ├── episodes/episode_XXXXXX/
    │     ├── camera_front/frame_000000.jpg ...
    │     ├── camera_back/ ... camera_top/
    │     ├── joint_states.parquet
    │     └── metadata.json
    └── classification/{good,defect}/<object_id>/{front,back,left,right,top}.jpg

패턴: Strategy.
`DatasetWriter` 인터페이스 뒤에 EpisodeWriter(ACT용 시퀀스)와
ClassificationWriter(CNN용 정지 이미지)를 둔다. 두 소비자의 요구가 완전히
다르기 때문에(시퀀스 vs 단일 스냅샷, parquet vs 폴더 라벨) 한 클래스에
섞으면 조건 분기가 폭발한다. Recorder 는 writer 목록을 순회할 뿐이다.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

import cv2
import numpy as np
import pandas as pd

from ..utils.errors import ValidationError

logger = logging.getLogger(__name__)

# 경로 조작(../) 과 공백을 막는다. 사용자 입력이 그대로 디렉터리명이 되므로 필수.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def validate_id(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not SAFE_ID_RE.match(value):
        raise ValidationError(
            f"{field_name} 는 영문/숫자/_/- 만 사용하고 64자 이내여야 합니다: '{value}'"
        )
    return value


@dataclass
class Sample:
    """한 타임스텝의 관측치."""

    t: float                                  # 에피소드 시작 기준 경과 시간(초)
    frames: dict[str, np.ndarray]             # camera name → BGR image
    frame_timestamps: dict[str, float]        # camera name → monotonic ts
    leader_joints: list[float]
    follower_joints: list[float]
    sync_skew_ms: float


@dataclass
class EpisodeMeta:
    episode_id: str
    object_id: str
    label: str                                # "good" | "defect"
    defect_type: str | None
    fps: int
    cameras: Sequence[str]
    joint_names: Sequence[str]
    started_at_epoch: float
    monotonic_offset: float                   # epoch = monotonic + offset (복원용)
    extra: dict = field(default_factory=dict)


class DatasetWriter(Protocol):
    def open(self, meta: EpisodeMeta) -> None: ...
    def write(self, sample: Sample) -> None: ...
    def close(self, meta: EpisodeMeta, stats: dict) -> None: ...
    def abort(self) -> None: ...


class EpisodeWriter:
    """ACT 학습용 시퀀스 저장.

    이미지는 프레임 단위 JPEG, 관절값은 parquet 1개.
    parquet 을 고른 이유: CSV 대비 부동소수 정밀도 손실이 없고, LeRobot 계열
    로더가 pyarrow 로 바로 mmap 할 수 있어 수천 에피소드에서도 로딩이 싸다.
    """

    def __init__(self, root: Path, jpeg_quality: int = 90) -> None:
        self._root = root
        self._quality = jpeg_quality
        self._dir: Path | None = None
        self._rows: list[dict] = []
        self._frame_index = 0

    @property
    def directory(self) -> Path | None:
        return self._dir

    def open(self, meta: EpisodeMeta) -> None:
        target = self._root / "episodes" / f"episode_{meta.episode_id}"
        # exist_ok=False: 같은 ID 로 덮어써서 기존 에피소드를 조용히 날리는 사고를 막는다.
        try:
            target.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ValidationError(
                f"에피소드 '{meta.episode_id}' 가 이미 존재합니다. 다른 ID 를 사용하세요."
            ) from exc

        for cam in meta.cameras:
            (target / f"camera_{cam}").mkdir(parents=True, exist_ok=True)

        self._dir = target
        self._rows = []
        self._frame_index = 0

    def write(self, sample: Sample) -> None:
        assert self._dir is not None, "open() 이 선행되어야 합니다"
        idx = self._frame_index
        filename = f"frame_{idx:06d}.jpg"

        for cam, image in sample.frames.items():
            cv2.imwrite(
                str(self._dir / f"camera_{cam}" / filename),
                image,
                [int(cv2.IMWRITE_JPEG_QUALITY), self._quality],
            )

        row: dict = {
            "frame_index": idx,
            "timestamp": round(sample.t, 6),
            "sync_skew_ms": round(sample.sync_skew_ms, 3),
        }
        for j, val in enumerate(sample.leader_joints):
            row[f"leader_joint_{j}"] = val
        for j, val in enumerate(sample.follower_joints):
            row[f"follower_joint_{j}"] = val
        for cam, ts in sample.frame_timestamps.items():
            row[f"ts_{cam}"] = round(ts, 6)

        self._rows.append(row)
        self._frame_index += 1

    def close(self, meta: EpisodeMeta, stats: dict) -> None:
        assert self._dir is not None
        df = pd.DataFrame(self._rows)
        df.to_parquet(self._dir / "joint_states.parquet", index=False)

        payload = {
            "episode_id": meta.episode_id,
            "object_id": meta.object_id,
            "label": meta.label,
            "defect_type": meta.defect_type,
            "fps": meta.fps,
            "cameras": list(meta.cameras),
            "joint_names": list(meta.joint_names),
            "num_frames": len(self._rows),
            "started_at_epoch": meta.started_at_epoch,
            "monotonic_offset": meta.monotonic_offset,
            **stats,
            **meta.extra,
        }
        (self._dir / "metadata.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("에피소드 저장 완료: %s (%d frames)", self._dir, len(self._rows))

    def abort(self) -> None:
        """중단된 에피소드는 반쯤 쓰인 채 남기지 않고 통째로 지운다.

        반만 남은 에피소드는 학습 시 조용히 품질을 떨어뜨리는 최악의 데이터다.
        """
        if self._dir and self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
            logger.warning("에피소드 중단, 디렉터리 삭제: %s", self._dir)
        self._dir = None
        self._rows = []


class ClassificationWriter:
    """CNN 분류용 정지 이미지 저장.

    에피소드 전체가 아니라 '대표 한 세트'만 남긴다. 시퀀스 프레임을 전부
    분류 데이터로 쓰면 거의 동일한 이미지가 수천 장 들어가 과적합과
    train/val 누수를 부른다.
    """

    def __init__(self, root: Path, capture_at: float = 0.5, jpeg_quality: int = 95) -> None:
        self._root = root
        self._capture_at = capture_at
        self._quality = jpeg_quality
        self._captured = False
        self._pending: dict[str, np.ndarray] = {}
        self._dir: Path | None = None

    def open(self, meta: EpisodeMeta) -> None:
        label_dir = "defect" if meta.label == "defect" else "good"
        self._dir = self._root / "classification" / label_dir / meta.object_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._captured = False
        self._pending = {}

    def write(self, sample: Sample) -> None:
        # 진행률 기반 선택은 총 길이를 미리 모르므로, 마지막 샘플을 계속 갱신하다가
        # close() 시점에 확정한다. 결과적으로 '가장 최근 정상 프레임'이 저장된다.
        if sample.frames:
            self._pending = {k: v.copy() for k, v in sample.frames.items()}

    def close(self, meta: EpisodeMeta, stats: dict) -> None:
        if not self._pending or self._dir is None:
            logger.warning("분류 이미지로 저장할 프레임이 없습니다: %s", meta.episode_id)
            return

        for cam, image in self._pending.items():
            # 같은 object_id 로 여러 번 스캔할 수 있으므로 에피소드 ID 를 접미사로 붙인다.
            path = self._dir / f"{cam}.jpg"
            if path.exists():
                path = self._dir / f"{cam}_{meta.episode_id}.jpg"
            cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality])

        label_meta = self._dir / "labels.json"
        history = json.loads(label_meta.read_text(encoding="utf-8")) if label_meta.exists() else []
        history.append(
            {
                "episode_id": meta.episode_id,
                "label": meta.label,
                "defect_type": meta.defect_type,
                "captured_at_epoch": meta.started_at_epoch,
            }
        )
        label_meta.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        self._captured = True

    def abort(self) -> None:
        self._pending = {}


class DatasetIndex:
    """수집 현황 집계. UI 하단의 에피소드 카운트를 위한 읽기 전용 뷰."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def summary(self) -> dict:
        with self._lock:
            episodes_dir = self._root / "episodes"
            classification_dir = self._root / "classification"

            episodes = sorted(p.name for p in episodes_dir.glob("episode_*") if p.is_dir())
            good = len(list((classification_dir / "good").glob("*"))) if (classification_dir / "good").exists() else 0
            defect = len(list((classification_dir / "defect").glob("*"))) if (classification_dir / "defect").exists() else 0

            return {
                "total_episodes": len(episodes),
                "recent_episodes": episodes[-5:][::-1],
                "good_objects": good,
                "defect_objects": defect,
            }

    def next_episode_id(self) -> str:
        """episode_000042 다음은 000043. 빈 슬롯을 재사용하지 않는다."""
        with self._lock:
            episodes_dir = self._root / "episodes"
            if not episodes_dir.exists():
                return "000000"
            numbers = []
            for path in episodes_dir.glob("episode_*"):
                suffix = path.name.replace("episode_", "")
                if suffix.isdigit():
                    numbers.append(int(suffix))
            return f"{(max(numbers) + 1) if numbers else 0:06d}"
