"""데이터셋 저장 계층.

요구된 폴더 구조를 그대로 만든다:

    lerobot_data/
    ├── episodes/episode_XXXXXX/
    │     ├── camera_cam1/frame_000000.jpg ...
    │     ├── camera_cam2/ ... camera_cam5/
    │     ├── joint_states.parquet
    │     └── metadata.json
    └── classification/{good,defect}/<object_id>/{cam1,cam2,cam3,cam4,cam5}.jpg

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

from ..config import DEFECT_TYPES
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


def imwrite(path: Path, image: np.ndarray, quality: int) -> None:
    """JPEG 저장. cv2.imwrite 를 직접 쓰지 않는다.

    cv2.imwrite/imread 는 경로를 ANSI 코드페이지로 해석해 비ASCII 경로에서
    **조용히 False 를 반환**한다(예외도 없다). 이 프로젝트는 `바탕 화면` 같은
    한글 경로 아래에서 도는 게 정상 사용이라, 그대로 두면 에피소드가 프레임
    0장으로 저장된다. 인코딩과 파일 쓰기를 분리해 파이썬이 경로를 다루게 한다.
    """
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise OSError(f"JPEG 인코딩 실패: {path}")
    path.write_bytes(buf.tobytes())


def imread(path: Path) -> np.ndarray | None:
    """JPEG 로드. imwrite 와 같은 이유로 cv2.imread 를 쓰지 않는다."""
    if not path.exists():
        return None
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


def validate_label(label: str, defect_type: str | None) -> tuple[str, str | None]:
    """라벨 쌍 검증. 녹화 시작(Recorder)과 사후 수정(DatasetIndex.update)이 공유한다.

    한쪽에만 두면 수정 API 로 'good + scratch' 같은 오염된 조합이 들어온다.
    """
    label = (label or "").lower()
    if label not in {"good", "defect"}:
        raise ValidationError("label 은 'good' 또는 'defect' 여야 합니다.")
    if label == "defect":
        if defect_type not in DEFECT_TYPES:
            raise ValidationError(
                f"defect_type 은 {', '.join(DEFECT_TYPES)} 중 하나여야 합니다."
            )
        return label, defect_type
    return label, None  # GOOD 인데 defect_type 이 남아있으면 라벨 오염


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
            imwrite(self._dir / f"camera_{cam}" / filename, image, self._quality)

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

    대표 프레임은 '마지막 정상 프레임'이다. 에피소드 총 길이를 미리 알 수 없어
    "50% 지점" 같은 진행률 기준을 스트리밍 중에 고를 수 없기 때문이다.
    """

    def __init__(self, root: Path, jpeg_quality: int = 95) -> None:
        self._root = root
        self._quality = jpeg_quality
        self._pending: dict[str, np.ndarray] = {}
        self._dir: Path | None = None

    def open(self, meta: EpisodeMeta) -> None:
        label_dir = "defect" if meta.label == "defect" else "good"
        self._dir = self._root / "classification" / label_dir / meta.object_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pending = {}

    def write(self, sample: Sample) -> None:
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
            imwrite(path, image, self._quality)

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

    def abort(self) -> None:
        self._pending = {}


class DatasetIndex:
    """수집 현황 집계 + 저장된 에피소드의 사후 편집.

    라벨은 사람이 눈으로 보고 정하므로 반드시 틀린다. 잘못 붙은 라벨을
    고치려고 폴더를 손으로 옮기면 metadata.json 과 분류 폴더가 어긋나므로,
    두 곳을 한 번에 바꾸는 경로를 여기 하나만 둔다.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 편집
    def list(self) -> list[dict]:
        """metadata.json 목록. 중단되어 메타가 없는 디렉터리는 건너뛴다."""
        out: list[dict] = []
        for path in sorted((self._root / "episodes").glob("episode_*")):
            meta_file = path / "metadata.json"
            if not meta_file.exists():
                continue
            try:
                out.append(json.loads(meta_file.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                logger.warning("metadata.json 파손, 목록에서 제외: %s", meta_file)
        return out

    def update(
        self,
        episode_id: str,
        *,
        object_id: str | None = None,
        label: str | None = None,
        defect_type: str | None = None,
    ) -> dict:
        """라벨/물체 ID 수정. None 인 필드는 기존 값을 유지한다."""
        meta_file = self._meta_path(episode_id)
        with self._lock:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))

            new_object = validate_id(object_id, "object_id") if object_id is not None else meta["object_id"]
            new_label, new_defect = validate_label(
                meta["label"] if label is None else label,
                meta.get("defect_type") if defect_type is None else defect_type,
            )

            # 시연(kind=demo) 에피소드의 label 은 Recorder 를 통과시키려 채운 자리표시다. 분류 이미지를 만든 적이
            # 없는데 같은 (label, object_id) 폴더를 옮기면 다른 에피소드가 남긴 분류 이미지가 사라진다.
            if meta.get("kind") != "demo":
                self._move_classification(meta["label"], meta["object_id"], new_label, new_object)
            meta.update(object_id=new_object, label=new_label, defect_type=new_defect)
            meta_file.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("에피소드 수정: %s → %s/%s", episode_id, new_label, new_object)
            return meta

    def delete(self, episode_id: str) -> None:
        meta_file = self._meta_path(episode_id)
        target = meta_file.parent
        with self._lock:
            # 삭제 전에 라벨/물체를 읽어둔다 — 지우고 나면 알 수 없다.
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            shutil.rmtree(target)
            logger.warning("에피소드 삭제: %s", target)
            if meta.get("kind") == "demo":    # 시연 에피소드는 분류 이미지를 만든 적이 없다(update 주석 참고)
                return

            # 같은 (label, object_id) 를 참조하는 형제 에피소드가 남아 있으면
            # 분류 이미지는 공유 자산이라 둔다. 마지막 참조가 사라졌으면 함께
            # 지운다 — 남겨두면 3D 검사 드롭다운에 유령 물체로 계속 뜬다.
            # 시연 에피소드는 참조로 세지 않는다 — 자리표시 라벨이 분류 이미지를 붙잡아 둔다.
            still_referenced = any(
                m["label"] == meta["label"] and m["object_id"] == meta["object_id"] and m.get("kind") != "demo"
                for m in self.list()
            )
            if not still_referenced:
                stills = self._root / "classification" / meta["label"] / meta["object_id"]
                if stills.exists():
                    shutil.rmtree(stills, ignore_errors=True)
                    logger.warning("고아 분류 이미지 삭제: %s", stills)

    def _meta_path(self, episode_id: str) -> Path:
        episode_id = validate_id(episode_id, "episode_id")
        meta_file = self._root / "episodes" / f"episode_{episode_id}" / "metadata.json"
        if not meta_file.exists():
            raise ValidationError(f"에피소드 '{episode_id}' 를 찾을 수 없습니다.")
        return meta_file

    def _move_classification(self, old_label, old_object, new_label, new_object) -> None:
        """분류 이미지 폴더를 함께 옮긴다.

        메타만 고치면 classification/good/<obj> 에 불량 사진이 남아 CNN 학습셋이
        조용히 오염된다. 라벨 수정의 절반을 빼먹는 것과 같다.

        ponytail: 폴더 단위로 옮기므로 같은 object_id 를 공유하는 형제 에피소드의
        metadata.json 은 따라오지 않는다. 형제까지 일괄 갱신은 필요해지면 추가.
        """
        if (old_label, old_object) == (new_label, new_object):
            return
        src = self._root / "classification" / old_label / old_object
        if not src.exists():
            return
        dst = self._root / "classification" / new_label / new_object
        if dst.exists():
            raise ValidationError(
                f"'{new_label}/{new_object}' 분류 폴더가 이미 있습니다. "
                "다른 object_id 를 쓰거나 기존 폴더를 먼저 정리하세요."
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

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
