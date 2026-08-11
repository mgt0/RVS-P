"""ACT 학습 · 3D 재구성 API."""

from __future__ import annotations

import json
import time

import numpy as np
from flask import Blueprint, Response, current_app, jsonify, request

from ..core.reconstruct3d import (
    _extract_silhouette as extract_silhouette,
)
from ..core.reconstruct3d import load_backgrounds, load_object_views, reconstruct_from_views
from ..core.cad import compare_with_scan, load_reference, save_model
from ..core.storage import imwrite, validate_id
from ..utils.errors import ValidationError

bp = Blueprint("analysis", __name__, url_prefix="/api")

# 업로드 상한. 이보다 큰 STL 은 브라우저에서 렌더링도 버겁다.
MAX_MODEL_BYTES = 50_000_000


def _hub():
    return current_app.extensions["omx_hub"]


# ══════════════════════════ ACT 학습 ══════════════════════════
@bp.post("/train/start")
def train_start():
    body = request.json or {}
    # demo=True 가 기본. 실제 lerobot 학습은 GPU/데이터셋이 준비된 환경에서 demo=False.
    demo = bool(body.get("demo", True))
    params = {
        "epochs": int(body.get("epochs", 50)),
        "num_joints": int(body.get("num_joints", 5)),
        "chunk_size": int(body.get("chunk_size", 32)),
        "lr": float(body.get("lr", 1e-4)),
        "threshold_deg": float(body.get("threshold_deg", 5.0)),
        "epoch_delay_sec": float(body.get("epoch_delay_sec", 0.3)),
        "seed": int(body.get("seed", 42)),
    }
    try:
        status = _hub().training.start(params, demo=demo)
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc
    return jsonify({"ok": True, "training": status})


@bp.post("/train/stop")
def train_stop():
    try:
        status = _hub().training.stop()
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc
    return jsonify({"ok": True, "training": status})


@bp.get("/train/status")
def train_status():
    return jsonify({"ok": True, "training": _hub().training.status()})


@bp.get("/train/stream")
def train_stream():
    """SSE 실시간 메트릭 스트림.

    폴링 대신 SSE 를 쓰는 이유: 에폭이 끝날 때마다 즉시 밀어줘야 차트가
    '자라나는' 느낌이 난다. 폴링은 간격만큼 지연되고 빈 응답이 낭비된다.
    """
    manager = _hub().training

    def generate():
        last_count = -1
        # 연결 유지 한계: 학습이 끝나거나 클라이언트가 끊으면 종료.
        for _ in range(100_000):
            status = manager.status()
            count = status.get("num_epochs", 0)
            state = status.get("state")

            if count != last_count or state in {"DONE", "STOPPED", "FAILED"}:
                last_count = count
                yield f"data: {json.dumps(status)}\n\n"

            if state in {"DONE", "STOPPED", "FAILED", "IDLE"} and count == last_count:
                # 종료 상태를 한 번 더 보낸 뒤 스트림을 닫는다.
                if state in {"DONE", "STOPPED", "FAILED"}:
                    yield f"data: {json.dumps(status)}\n\n"
                break
            time.sleep(0.25)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ══════════════════════════ 3D 재구성 ══════════════════════════
@bp.get("/scan/objects")
def scan_objects():
    """재구성 가능한 스캔 물체 목록(classification 폴더 기준)."""
    root = _hub().cfg.recording.root
    items = []
    for label in ("good", "defect"):
        label_dir = root / "classification" / label
        if not label_dir.exists():
            continue
        for obj in sorted(p for p in label_dir.iterdir() if p.is_dir()):
            views = sorted({p.stem.split("_")[0] for p in obj.glob("*.jpg")})
            items.append({"object_id": obj.name, "label": label, "views": views})
    return jsonify({"ok": True, "objects": items})


@bp.post("/scan/background")
def scan_background():
    """지금 보이는 화면을 '빈 스캔대'로 저장한다. 물체를 치우고 한 번만 누르면 된다.

    실루엣 추출이 흑백 Otsu 만으로는 이 리그에서 동작하지 않는다(배경에 선반·창문·
    현수막이 들어와 물체보다 크게 잡힌다). 배경을 알고 있으면 무엇이 놓였는지가
    아니라 '무엇이 달라졌는지'만 보면 되므로 배경이 복잡해도 상관없어진다.

    조명이나 카메라 위치가 바뀌면 다시 찍어야 한다.
    """
    hub = _hub()
    frames, _ = hub.cameras.snapshot()
    if not frames:
        raise ValidationError("카메라에서 프레임을 받지 못했습니다. 연결 상태를 확인하세요.")

    bg_dir = hub.cfg.recording.root / "background"
    bg_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        imwrite(bg_dir / f"{name}.jpg", frame.image, 95)

    return jsonify({"ok": True, "cameras": sorted(frames), "path": str(bg_dir)})


@bp.get("/scan/model/<object_id>")
def scan_model(object_id: str):
    """양품 기준이 되는 CAD 모델(STL). lerobot_data/models/<object_id>.stl

    스캔으로 형상을 '만드는' 것보다 설계 형상을 그대로 쓰는 편이 정확하다.
    5뷰 visual hull 은 오목부를 복원하지 못하고 절대 치수도 없다.
    """
    hub = _hub()
    root = hub.cfg.recording.root
    mesh = load_reference(root, object_id)
    if mesh is None:
        raise ValidationError(
            f"'{object_id}' 의 기준 모델이 없습니다. "
            f"lerobot_data/models/{object_id}.stl 로 저장하세요. "
            "(STEP/STP 는 CAD 툴에서 STL 로 내보내 주세요)"
        )

    # 스캔 이미지가 있으면 설계와 다른 곳을 색으로 얹는다.
    label = request.args.get("label", "defect")
    try:
        views = load_object_views(root, label, object_id)
    except FileNotFoundError:
        views = {}

    if views:
        backgrounds = load_backgrounds(root)
        silhouettes = {
            n: extract_silhouette(img, backgrounds.get(n)) for n, img in views.items()
        }
        mesh = compare_with_scan(
            mesh,
            silhouettes,
            {c.name: c.view_dir for c in hub.cfg.cameras.values() if c.view_dir},
        )

    return jsonify({"ok": True, "mesh": mesh})


@bp.post("/scan/model/<object_id>")
def upload_model(object_id: str):
    """기준 CAD 모델(STL) 업로드. 대시보드의 '기준 모델 등록' 버튼이 쓴다."""
    object_id = validate_id(object_id, "object_id")

    upload = request.files.get("file")
    if upload is None:
        raise ValidationError("파일이 첨부되지 않았습니다.")

    # 한 바이트 더 읽어 초과 여부를 판정한다. 통째로 읽고 재면 그 시점에 이미
    # 메모리를 다 쓴 뒤다.
    raw = upload.read(MAX_MODEL_BYTES + 1)
    if len(raw) > MAX_MODEL_BYTES:
        raise ValidationError(
            f"파일이 너무 큽니다. {MAX_MODEL_BYTES // 1_000_000}MB 이하로 줄여주세요 "
            "(CAD 에서 내보낼 때 해상도를 낮추면 됩니다)."
        )

    try:
        info = save_model(_hub().cfg.recording.root, object_id, raw)
    except ValueError as exc:
        raise ValidationError(f"STL 로 읽을 수 없습니다: {exc}") from exc

    return jsonify({"ok": True, "object_id": object_id, **info})


@bp.post("/scan/reconstruct")
def scan_reconstruct():
    """지정한 물체의 5뷰로 3D 메시를 생성해 반환.

    응답은 three.js 가 바로 먹는 vertices/faces/vertex_colors.
    큰 메시는 JSON 이 무거우므로 resolution 으로 조절한다.
    """
    body = request.json or {}
    object_id = body.get("object_id")
    label = body.get("label", "good")
    resolution = min(max(int(body.get("resolution", 48)), 16), 96)

    if not object_id:
        raise ValidationError("object_id 가 필요합니다.")

    hub = _hub()
    try:
        views = load_object_views(hub.cfg.recording.root, label, object_id)
    except FileNotFoundError as exc:
        raise ValidationError(str(exc)) from exc

    # 불량 영역: 실제로는 CNN/이상탐지 결과가 들어온다. 데모에서는
    # defect 라벨이면 대표 위치 하나를 표시(프론트에서 확인용).
    classification = body.get("classification") or {
        "label": label,
        "confidence": float(body.get("confidence", 0.0)),
    }
    defect_regions = body.get("defect_regions")
    if defect_regions is None and label == "defect":
        # 실제로는 CNN Grad-CAM 이나 이상탐지 히트맵의 3D 역투영 위치가 들어온다.
        # 기본값은 표면 위(전면 상단)에 둬 하이라이트가 보이도록 한다.
        defect_regions = [{"center": [0.35, 0.45, 0.6], "radius": 0.6, "severity": 0.9}]

    result = reconstruct_from_views(
        views,
        resolution=resolution,
        defect_regions=defect_regions,
        classification=classification,
        # 빈 스캔대 참조가 있으면 배경 차분, 없으면 Otsu 폴백
        backgrounds=load_backgrounds(hub.cfg.recording.root),
        # 리그 기하는 물리적 사실이라 코드가 아니라 config 가 갖는다.
        directions={c.name: c.view_dir for c in hub.cfg.cameras.values() if c.view_dir},
    )
    return jsonify({"ok": True, "mesh": result.to_dict()})


@bp.post("/scan/reconstruct_demo")
def scan_reconstruct_demo():
    """업로드/스캔 없이 즉시 확인용. 합성 5뷰(구·실루엣)로 재구성.

    카메라·물체 없이도 3D 뷰어와 하이라이트가 동작하는지 검증한다.
    """
    label = (request.json or {}).get("label", "defect")
    resolution = min(max(int((request.json or {}).get("resolution", 48)), 16), 96)

    # 합성 실루엣: 중앙에 blob 이 있는 흰 배경 이미지 5장
    size = 256
    views = {}
    rng = np.random.default_rng(7)
    for name in ("cam1", "cam2", "cam3", "cam4", "cam5"):
        img = np.full((size, size, 3), 240, np.uint8)
        cx, cy = size // 2, size // 2
        axes = (70 + rng.integers(-8, 8), 90 + rng.integers(-8, 8))
        import cv2
        cv2.ellipse(img, (cx, cy), axes, 0, 0, 360, (60, 60, 60), -1)
        views[name] = img

    # 불량 영역은 표면 위에 둔다. visual hull 은 표면 정점만 만들므로
    # 중심(0,0,0) 근처에 두면 물들 정점이 없다. 물체 앞면 바깥쪽에 배치.
    regions = [{"center": [0.35, 0.45, 0.6], "radius": 0.6, "severity": 0.9}] if label == "defect" else None
    result = reconstruct_from_views(
        views,
        resolution=resolution,
        defect_regions=regions,
        classification={"label": label, "confidence": 0.91},
    )
    return jsonify({"ok": True, "mesh": result.to_dict()})
