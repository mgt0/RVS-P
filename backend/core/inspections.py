"""검사 샘플(inspections/<capture_id>/) 라벨링과 물체·데이터 준비도 집계.

촬영 자체는 /api/scan/capture(analysis.py)가 한다. 여기서는 그 폴더를 읽고, 사람이 붙인 라벨을
폴더 안 label.json 에만 쓴다. classification/ 에는 절대 쓰지 않는다 — 그쪽은 에피소드 녹화가 만드는
CNN 학습셋이라 에피소드 수정·삭제(DatasetIndex)가 그 폴더를 옮기고 지운다. 검사 샘플 라벨이
섞이면 엉뚱한 폴더가 사라진다.

촬영본에는 주변 사람의 얼굴이 찍힐 수 있다 — 이미지는 이 서버의 이미지 라우트로만 내보낸다.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from ..utils.errors import OmxError, UnknownCameraError, ValidationError
from .storage import SAFE_ID_RE, validate_id, validate_label

logger = logging.getLogger(__name__)

# 폴더 이름이 곧 촬영 시각이다(analysis.py 의 run_scan_capture). 같은 초에 또 찍으면 덮어쓰지 않고 _02, _03 … 이
# 붙는다(학습 run_id 와 같은 방식 — 두 자리라 사전순 = 시간순). [0-9] 로 쓴 이유: \d 는 유니코드 숫자도 받는다.
CAPTURE_ID_RE = re.compile(r"[0-9]{8}_[0-9]{6}(?:_[0-9]{2,})?")
CAPTURE_STAMP_LEN = len("YYYYmmdd_HHMMSS")      # 접미사를 뗀 시각 부분의 길이
ORANGE_EVALS = ("right", "partial", "wrong")    # 주황(이상) 표시가 맞았는지에 대한 사람의 판정
ANOMALY_KEYS = ("flagged_ratio", "num_regions", "max_severity")
RECOMMENDED_POSES = 6                           # 물체 하나당 권장 촬영 자세 수
GOALS = {"act_episodes": 50, "objects_per_class": 50}   # ACT 는 태스크당 50 에피소드, 분류기는 클래스당 물체 50개가 통상 하한
CAD_SUFFIXES = (".stl", ".stp", ".step")        # cad.find_model 과 같은 규칙

_label_lock = threading.Lock()                  # pose_index 계산과 label.json 쓰기를 한 덩어리로


class InspectionNotFoundError(OmxError):
    status_code = 404
    code = "inspection_not_found"


# ------------------------------------------------------------------ 촬영 폴더
def _valid(capture_id: str) -> bool:
    if not CAPTURE_ID_RE.fullmatch(capture_id or ""):
        return False
    try:
        datetime.strptime(capture_id[:CAPTURE_STAMP_LEN], "%Y%m%d_%H%M%S")
    except ValueError:      # 99999999_999999 처럼 모양만 맞는 값
        return False
    return True


def capture_dir(root: Path, capture_id: str) -> Path:
    """검증된 촬영 폴더. 형식이 틀리면 400, 없으면 404.

    capture_id 는 URL 에서 그대로 들어온다. 정규식이 경로 구분자를 통째로 막아 ../ 가 끼어들 틈이 없다.
    """
    if not _valid(capture_id):
        raise ValidationError(f"capture_id 는 YYYYMMDD_HHMMSS(같은 초의 촬영은 _02 같은 접미사) 형식이어야 합니다: '{capture_id}'")
    directory = root / "inspections" / capture_id
    if not directory.is_dir():
        raise InspectionNotFoundError(f"촬영 '{capture_id}' 를 찾을 수 없습니다.")
    return directory


def read_label(directory: Path) -> dict | None:
    path = directory / "label.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("label.json 파손, 라벨 없음으로 취급: %s", path)
        return None
    return data if isinstance(data, dict) else None


def _entry(directory: Path, slots) -> dict:
    have = {p.stem for p in directory.glob("*.jpg")}
    return {
        "capture_id": directory.name,
        "captured_at": datetime.strptime(directory.name[:CAPTURE_STAMP_LEN], "%Y%m%d_%H%M%S").isoformat(timespec="seconds"),
        "cameras": [s for s in slots if s in have],     # 설정 슬롯 순서대로
        "label": read_label(directory),
    }


def all_captures(root: Path, slots) -> list[dict]:
    """전체 촬영, 최신순. 폴더 이름이 촬영 시각이라 이름 정렬이 곧 시간 정렬이다."""
    base = root / "inspections"
    if not base.is_dir():
        return []
    dirs = sorted((p for p in base.iterdir() if p.is_dir() and _valid(p.name)), key=lambda p: p.name, reverse=True)
    return [_entry(p, slots) for p in dirs]


def image_path(root: Path, slots, capture_id: str, cam: str) -> Path:
    directory = capture_dir(root, capture_id)
    if cam not in slots:
        raise UnknownCameraError(f"등록되지 않은 카메라 '{cam}'. 사용 가능: {', '.join(slots)}")
    path = directory / f"{cam}.jpg"
    if not path.is_file():
        raise InspectionNotFoundError(f"촬영 '{capture_id}' 에 {cam} 이미지가 없습니다.")
    return path


# ------------------------------------------------------------------ 라벨
def _clean_anomaly(value) -> dict | None:
    """3D 결과의 이상 통계. 나중에 주황 표시의 정확도를 재려고 판정과 함께 남긴다."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("anomaly 는 객체여야 합니다.")
    out = {}
    for key in ANOMALY_KEYS:
        num = value.get(key)
        if num is None:
            continue
        if isinstance(num, bool) or not isinstance(num, (int, float)) or not math.isfinite(num):
            raise ValidationError(f"anomaly.{key} 는 숫자여야 합니다: {num!r}")
        out[key] = num
    return out or None


def _next_pose_index(root: Path, object_id: str, exclude: str) -> int:
    """이 물체의 라벨된 다른 촬영 중 가장 큰 자세 번호 + 1. 개수 + 1 이 아닌 이유: 한 촬영을 다른
    물체로 옮겨 라벨하면 번호가 비는데, 개수 기준이면 남은 촬영과 번호가 겹친다."""
    base = root / "inspections"
    top = 0
    for directory in base.iterdir() if base.is_dir() else ():
        if directory.name == exclude or not directory.is_dir():
            continue
        label = read_label(directory)
        if label and label.get("object_id") == object_id and isinstance(label.get("pose_index"), int):
            top = max(top, label["pose_index"])
    return top + 1


def set_label(root: Path, slots, capture_id: str, *, object_id, label, defect_type, orange_eval, anomaly) -> dict:
    directory = capture_dir(root, capture_id)
    object_id = validate_id(object_id, "object_id")
    label, defect_type = validate_label(label, defect_type)
    if orange_eval is not None and orange_eval not in ORANGE_EVALS:
        raise ValidationError(f"orange_eval 은 {', '.join(ORANGE_EVALS)} 중 하나이거나 null 이어야 합니다.")
    anomaly = _clean_anomaly(anomaly)

    with _label_lock:
        previous = read_label(directory)
        if previous and previous.get("object_id") == object_id and isinstance(previous.get("pose_index"), int):
            pose_index = previous["pose_index"]     # 같은 물체로 다시 저장하면 자세 번호를 유지한다
        else:
            pose_index = _next_pose_index(root, object_id, exclude=capture_id)
        record = {
            "object_id": object_id,
            "label": label,
            "defect_type": defect_type,
            "pose_index": pose_index,
            "orange_eval": orange_eval,
            "anomaly": anomaly,
            "labeled_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 쓰다 끊겨 반쪽 JSON 이 남지 않게 임시 파일에 쓴 뒤 바꿔치기한다.
        tmp = directory / "label.json.tmp"
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(directory / "label.json")
    logger.info("검사 라벨: %s → %s/%s (자세 %d)", capture_id, label, object_id, pose_index)
    return _entry(directory, slots)


def delete_capture(root: Path, capture_id: str) -> None:
    """라벨 없는 촬영만 지운다. 라벨이 붙은 촬영은 판정 근거라 막는다."""
    directory = capture_dir(root, capture_id)
    with _label_lock:
        if read_label(directory) is not None:
            raise ValidationError("라벨이 붙은 촬영은 삭제할 수 없습니다. 판정 근거로 보존됩니다.")
        shutil.rmtree(directory)
    logger.warning("검사 촬영 삭제: %s", directory)


# ------------------------------------------------------------------ 집계
def object_counts(episodes: list[dict], captures: list[dict]) -> dict:
    """양품·불량 물체 수(물체 ID 중복 제외).

    분류 라벨이 있는 것은 kind 없는(기존 방식으로 녹화한) 에피소드와 라벨된 검사 촬영뿐이다.
    시연 에피소드(kind=demo)의 label 은 Recorder 를 통과시키려 채운 자리표시라 세지 않는다.
    """
    seen: dict[str, set] = {"good": set(), "defect": set()}
    for meta in episodes:
        if not meta.get("kind") and meta.get("label") in seen:
            seen[meta["label"]].add(meta.get("object_id"))
    for entry in captures:
        label = entry["label"]
        if label and label.get("label") in seen:
            seen[label["label"]].add(label.get("object_id"))
    return {name: len(ids) for name, ids in seen.items()}


def summary(episodes: list[dict], captures: list[dict]) -> dict:
    unlabeled = sum(1 for entry in captures if not entry["label"])
    return {"total": len(captures), "unlabeled": unlabeled, "objects": object_counts(episodes, captures)}


def readiness(episodes: list[dict], captures: list[dict]) -> dict:
    counts = object_counts(episodes, captures)
    return {
        "goals": GOALS,
        "act_episodes": len(episodes),      # 시연(kind=demo)과 기존 에피소드 모두 ACT 학습에 쓴다
        "good_objects": counts["good"],
        "defect_objects": counts["defect"],
    }


def object_list(root: Path, episodes: list[dict], captures: list[dict]) -> list[dict]:
    """물체 선택 목록: 에피소드·라벨된 촬영·CAD 모델 어디든 한 번이라도 나온 물체.

    poses·labels 는 라벨된 검사 촬영만 센다(poses = labels.good + labels.defect).
    """
    ids: set[str] = {m["object_id"] for m in episodes if m.get("object_id")}
    labels: dict[str, dict[str, int]] = {}
    for entry in captures:
        label = entry["label"]
        if label and label.get("label") in ("good", "defect") and label.get("object_id"):
            ids.add(label["object_id"])
            labels.setdefault(label["object_id"], {"good": 0, "defect": 0})[label["label"]] += 1

    cad: set[str] = set()
    models = root / "models"
    for path in models.iterdir() if models.is_dir() else ():
        if path.is_file() and path.suffix.lower() in CAD_SUFFIXES:
            cad.add(path.stem.lower())
            if SAFE_ID_RE.match(path.stem):     # 화면에서 쓸 수 없는 이름은 목록에 올리지 않는다
                ids.add(path.stem)

    out = []
    for object_id in sorted(ids, key=str.lower):
        counts = labels.get(object_id, {"good": 0, "defect": 0})
        out.append({
            "object_id": object_id,
            "has_cad": object_id.lower() in cad,
            "poses": counts["good"] + counts["defect"],
            "labels": counts,
        })
    return out
