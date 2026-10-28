"""ACT 학습 · 3D 재구성 API."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Blueprint, Response, jsonify, request

from ..core import inspections
from ..core.cycle import CycleBusyError
from ..core.reconstruct3d import (
    consistent_views,
    extract_silhouette,
    load_backgrounds,
    load_object_views,
    reconstruct_from_views,
)
from ..core.cad import compare_with_scan, load_reference, save_model
from ..core.rig_calibration import (
    CUBE_MM,
    MARKER_MM,
    calibrate,
    clear_shots,
    load_rig,
    load_shots,
    rig_summary,
    save_rig,
    save_shot,
)
from ..core.storage import imread, imwrite, validate_id
from ..utils.errors import ValidationError
from . import current_hub

bp = Blueprint("analysis", __name__, url_prefix="/api")

# 업로드 상한. 이보다 큰 STL 은 브라우저에서 렌더링도 버겁다.
MAX_MODEL_BYTES = 50_000_000


def _scan_geometry(hub) -> dict:
    """재구성에 넘길 리그 기하: 공칭 시선 방향, 검사 ROI, 캘리브레이션.

    시뮬레이션 프레임은 실제 리그를 찍은 게 아니다(화면을 가로지르는 합성 원). 리그에 맞춘
    ROI·캘리브레이션을 대면 물체가 ROI 밖으로 나가 형상이 비므로 시뮬레이션에서는 쓰지 않는다.
    """
    cams = [c for c in hub.cfg.cameras.values() if c.view_dir]
    sim = hub.cfg.server.simulate
    return {
        "directions": {c.name: c.view_dir for c in cams},
        "rois": {} if sim else {c.name: c.roi for c in cams if c.roi},
        "rig": None if sim else load_rig(hub.cfg.recording.root),
    }


def _wait_belts_still(hub, settle: float) -> None:
    """벨트가 서 있는지 확인하고 진동이 가라앉기를 기다린 뒤 다시 확인한다. 모터는 건드리지 않는다.

    벨트가 돌면 모션 블러로 실루엣·마커가 번진다.
    """
    def _moving() -> bool:
        return any(c.status().get("running") for c in hub.conveyors.values())

    if _moving():
        raise ValidationError("컨베이어가 움직이는 중입니다 — 멈춘 뒤 촬영합니다")
    time.sleep(settle)
    if _moving():
        raise ValidationError("컨베이어가 움직이는 중입니다 — 멈춘 뒤 촬영합니다")


def _scan_frames(hub) -> dict:
    """재구성용 카메라(view_dir 있는 것)의 현재 프레임. 글로벌 캠(C270)은 제외."""
    frames, _ = hub.cameras.snapshot()
    if not frames:
        raise ValidationError("카메라에서 프레임을 받지 못했습니다. 연결 상태를 확인하세요.")
    names = {c.name for c in hub.cfg.cameras.values() if c.view_dir}
    return {n: f.image for n, f in frames.items() if n in names}


def _num(source, key: str, default, cast):
    """요청 값을 숫자로 바꾼다. 숫자가 아니면(문자열·null·NaN) 500 이 아니라 400 이다."""
    raw = source.get(key, default)
    try:
        value = cast(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f"{key} 는 숫자여야 합니다: {raw!r}") from None
    if not math.isfinite(value):
        raise ValidationError(f"{key} 는 유한한 숫자여야 합니다: {raw!r}")
    return value


def _no_cycle(hub, what: str) -> None:
    """사이클이 도는 동안의 수동 촬영은 거부한다. 사이클 도중에 배경을 찍으면 벨트 위 물체가 배경에 박혀 이후
    판정이 모두 틀어진다. 사이클 자신은 run_scan_capture 를 직접 부르므로 이 가드를 지나지 않는다."""
    if hub.cycle.active:
        raise CycleBusyError(what)


def _new_capture_dir(root: Path) -> tuple[str, Path]:
    """inspections/<시각>/ 폴더를 새로 만든다. 같은 초에 또 찍으면(사이클 검사와 수동 촬영 등) 덮어쓰지 않고
    _02, _03 … 을 붙인다. 학습 run_id 와 같은 방식이다 — 두 자리라 사전순이 시간순을 지킨다(inspections.CAPTURE_ID_RE)."""
    base = root / "inspections"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    capture_id, n = stamp, 1
    while True:
        try:
            (base / capture_id).mkdir()
            return capture_id, base / capture_id
        except FileExistsError:
            n += 1
            capture_id = f"{stamp}_{n:02d}"


def _reconstruct_scan(hub, views: dict, resolution: int) -> dict:
    """촬영본(재구성용 카메라 뷰)으로 형상을 만든다. 촬영 직후와 저장된 촬영을 다시 열 때가 같은 배경·리그 기하를 쓴다."""
    try:
        return reconstruct_from_views(
            views,
            resolution=resolution,
            classification={"label": "unknown", "confidence": 0.0},
            backgrounds=load_backgrounds(hub.cfg.recording.root),
            **_scan_geometry(hub),
        ).to_dict()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _capture_views(hub, capture_id: str) -> dict:
    """inspections/<capture_id>/ 에 저장된 재구성용 카메라 이미지. 형식이 틀리면 400, 폴더가 없으면 404.

    capture_id 는 요청 본문에서 그대로 들어와 파일 경로가 된다. 검증은 inspections.capture_dir 가 한다 —
    정규식이 경로 구분자를 통째로 막아 ../ 가 끼어들 틈이 없다(목록·이미지·라벨 라우트와 같은 규칙).
    """
    directory = inspections.capture_dir(hub.cfg.recording.root, capture_id)
    views = {}
    for cam in hub.cfg.cameras.values():
        if cam.view_dir and (img := imread(directory / f"{cam.name}.jpg")) is not None:
            views[cam.name] = img
    if len(views) < 2:
        raise ValidationError(f"촬영 '{capture_id}' 에서 읽을 수 있는 재구성용 이미지가 2장 미만입니다.")
    return views


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
        status = current_hub().training.start(params, demo=demo)
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc
    return jsonify({"ok": True, "training": status})


@bp.post("/train/stop")
def train_stop():
    try:
        status = current_hub().training.stop()
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc
    return jsonify({"ok": True, "training": status})


@bp.get("/train/status")
def train_status():
    return jsonify({"ok": True, "training": current_hub().training.status()})


@bp.get("/train/stream")
def train_stream():
    """SSE 실시간 메트릭 스트림.

    폴링 대신 SSE 를 쓰는 이유: 에폭이 끝날 때마다 즉시 밀어줘야 차트가
    '자라나는' 느낌이 난다. 폴링은 간격만큼 지연되고 빈 응답이 낭비된다.
    """
    manager = current_hub().training

    def generate():
        last_count, last_gpu = -1, None
        # 연결 유지 한계: 학습이 끝나거나 클라이언트가 끊으면 종료.
        for _ in range(100_000):
            status = manager.status()
            count = status.get("num_epochs", 0)
            state = status.get("state")
            gpu = status.get("gpu")

            # GPU 카드는 지표 줄(log_freq 스텝마다)보다 자주(5초마다) 바뀐다 — 그것만 바뀌어도 보낸다.
            if count != last_count or gpu != last_gpu or state in {"DONE", "STOPPED", "FAILED"}:
                last_count, last_gpu = count, gpu
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
    """재구성 가능한 스캔 물체 목록.

    classification 폴더만 보지 않고 에피소드 메타와 대조한다. 에피소드를
    지우면 그 물체는 목록에서도 사라져야 한다 — 폴더 기준으로만 나열하면
    지운 데이터가 드롭다운에 유령처럼 남는다. 이미지 파일 자체는 지우지
    않으므로(청소는 에피소드 삭제 쪽 책임) 목록 필터일 뿐이다.
    """
    hub = current_hub()
    root = hub.cfg.recording.root
    # 시연(kind=demo) 에피소드는 분류 이미지를 만들지 않는다. 그 자리표시 라벨을 살아 있는 참조로 세면
    # 지운 물체의 분류 폴더(예: good/cube)가 시연 한 번으로 목록에 되살아난다.
    alive = {(m["label"], m["object_id"]) for m in hub.recorder.index.list() if m.get("kind") != "demo"}

    items = []
    for label in ("good", "defect"):
        label_dir = root / "classification" / label
        if not label_dir.exists():
            continue
        for obj in sorted(p for p in label_dir.iterdir() if p.is_dir()):
            if (label, obj.name) not in alive:
                continue
            views = sorted({p.stem.split("_")[0] for p in obj.glob("*.jpg")})
            if views:
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
    hub = current_hub()
    _no_cycle(hub, "배경을 캡처")
    frames, _ = hub.cameras.snapshot()
    if not frames:
        raise ValidationError("카메라에서 프레임을 받지 못했습니다. 연결 상태를 확인하세요.")

    bg_dir = hub.cfg.recording.root / "background"
    bg_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        imwrite(bg_dir / f"{name}.jpg", frame.image, 95)

    return jsonify({"ok": True, "cameras": sorted(frames), "path": str(bg_dir)})


def run_scan_capture(hub, settle: float, resolution: int) -> dict:
    """컨베이어가 멈춘 지금, view_dir 이 있는 카메라로 한 세트를 찍어 visual hull 을 만든다.

    /api/scan/capture 와 자동 사이클(core/cycle.py)이 같이 쓴다. settle(0~3초)·resolution(16~96)은
    호출자가 범위를 맞춰 넘긴다.

    촬영만 한다 — 모터(벨트·팔)는 움직이지 않는다. 벨트가 돌고 있으면 모션 블러로
    실루엣이 번지므로 거부하고, settle 초 동안 진동이 가라앉기를 기다린 뒤 다시 확인한다.
    찍은 원본은 inspections/<시각>/ 에 남겨 검사 근거로 보존한다(같은 초의 촬영은 _02 같은 접미사).
    """
    _wait_belts_still(hub, settle)
    views = _scan_frames(hub)
    if len(views) < 2:
        raise ValidationError("촬영된 재구성용 카메라가 2대 미만입니다. 연결 상태를 확인하세요.")

    capture_id, out_dir = _new_capture_dir(hub.cfg.recording.root)
    for n, img in views.items():
        imwrite(out_dir / f"{n}.jpg", img, 95)

    return {
        "ok": True, "capture_id": capture_id, "cameras": sorted(views),
        "mesh": _reconstruct_scan(hub, views, resolution),
    }


@bp.post("/scan/capture")
def scan_capture():
    """컨베이어가 멈춘 지금 한 세트를 찍어 visual hull 을 만든다. 본문은 run_scan_capture.

    자동 사이클이 도는 동안은 거부한다(409) — 사이클은 run_scan_capture 를 직접 부른다.
    """
    hub = current_hub()
    _no_cycle(hub, "수동 촬영을 실행")
    body = request.json or {}
    settle = min(max(_num(body, "settle_sec", 0.5, float), 0.0), 3.0)
    resolution = min(max(_num(body, "resolution", 96, int), 16), 96)
    return jsonify(run_scan_capture(hub, settle, resolution))


# ── 리그 캘리브레이션(ArUco 마커 큐브) ──
# 절차: 마커 큐브를 정지 위치에 '앞(ID 2) 면을 앞으로' 놓고 샷 1장(기준) → 큐브를 조금씩 돌리고
# 한두 장은 기울여(하단 C1 의 초점거리용) 2~4장 더 → 계산. 첫 샷이 월드 좌표계가 된다.
# 촬영만 한다 — 모터는 움직이지 않는다.
@bp.get("/scan/calibration")
def calibration_status():
    return jsonify({"ok": True, **rig_summary(current_hub().cfg.recording.root)})


@bp.post("/scan/calibrate/shot")
def calibration_shot():
    hub = current_hub()
    _no_cycle(hub, "캘리브레이션 샷을 촬영")
    body = request.get_json(silent=True) or {}      # 버튼은 본문 없이 보낸다
    _wait_belts_still(hub, min(max(_num(body, "settle_sec", 0.5, float), 0.0), 3.0))
    views = _scan_frames(hub)
    if not views:
        raise ValidationError("재구성용 카메라 프레임이 없습니다. 연결 상태를 확인하세요.")
    index, detected = save_shot(hub.cfg.recording.root, views)
    return jsonify({
        "ok": True, "shot": index, "detected": detected,
        "pending_shots": index + 1,
        # 기준 샷에서 마커를 못 본 카메라는 보정할 수 없다 — 바로 알려 다시 놓게 한다.
        "missing": sorted(n for n, ids in detected.items() if not ids),
    })


@bp.post("/scan/calibrate/solve")
def calibration_solve():
    hub = current_hub()
    root = hub.cfg.recording.root
    body = request.get_json(silent=True) or {}
    cube_mm = float(body.get("cube_mm", CUBE_MM))
    marker_mm = float(body.get("marker_mm", MARKER_MM))
    if not (10.0 <= marker_mm < cube_mm <= 300.0):
        raise ValidationError("marker_mm 는 10 이상이고 cube_mm 보다 작아야 합니다(cube_mm ≤ 300).")

    shots = load_shots(root)
    if not shots:
        raise ValidationError("캘리브레이션 샷이 없습니다. 마커 큐브를 정지 위치에 놓고 먼저 촬영하세요.")
    rig, failed = calibrate(
        shots, cube_mm=cube_mm, marker_mm=marker_mm,
        nominal_dirs={c.name: c.view_dir for c in hub.cfg.cameras.values() if c.view_dir},
    )
    if len(rig.cameras) < 2:
        # 1대만 보정되면 원근 경로를 못 쓴다(2대 이상 필요). 반쪽짜리를 저장하지 않는다.
        raise ValidationError(
            "보정된 카메라가 2대 미만이라 저장하지 않았습니다. "
            + " · ".join(f"{n}: {why}" for n, why in sorted(failed.items()))
        )
    save_rig(root, rig, failed, marker_mm=marker_mm, shots=len(shots))
    return jsonify({"ok": True, **rig_summary(root)})


@bp.delete("/scan/calibrate/shots")
def calibration_clear_shots():
    """찍어 둔 샷만 지운다. 저장된 캘리브레이션(rig_calibration.json)은 그대로 둔다."""
    root = current_hub().cfg.recording.root
    clear_shots(root)
    return jsonify({"ok": True, **rig_summary(root)})


@bp.get("/scan/model/<object_id>")
def scan_model(object_id: str):
    """양품 기준이 되는 CAD 모델(STL). lerobot_data/models/<object_id>.stl

    스캔으로 형상을 '만드는' 것보다 설계 형상을 그대로 쓰는 편이 정확하다.
    5뷰 visual hull 은 오목부를 복원하지 못하고 절대 치수도 없다.
    """
    hub = current_hub()
    root = hub.cfg.recording.root
    mesh = load_reference(root, object_id)
    if mesh is None:
        raise ValidationError(
            f"'{object_id}' 의 기준 모델이 없습니다. "
            f"lerobot_data/models/{object_id}.stl 로 저장하세요. "
            "(.stp/.step 도 됩니다)"
        )

    # 스캔 이미지가 있으면 설계와 다른 곳(윤곽 결손 + 표면 이상)을 색으로 얹는다.
    label = request.args.get("label", "defect")
    try:
        views = load_object_views(root, label, object_id)
    except FileNotFoundError:
        views = {}

    if views:
        geom = _scan_geometry(hub)
        backgrounds = load_backgrounds(root)
        silhouettes = {
            n: extract_silhouette(img, backgrounds.get(n), geom["rois"].get(n))
            for n, img in views.items()
        }
        # 오염된 뷰 하나가 자세 정합을 통째로 망치지 않게 재구성과 같은 기준으로 뺀다.
        keep, dropped, _ = consistent_views(silhouettes, geom["directions"])
        mesh = compare_with_scan(
            mesh, {n: silhouettes[n] for n in keep}, geom["directions"],
            views={n: views[n] for n in keep},
        )
        mesh["stats"]["dropped_views"] = dropped
        reason = mesh["stats"].get("comparison_skipped")
        if reason:
            # 정합 못 한 CAD 를 회색으로 띄우면 '불량 없음'으로 읽힌다. 대신 스캔 형상과 그 위의
            # 표면 이상(주황)을 보여주고, CAD 비교를 못 한 이유를 같이 넘긴다(프론트가 알림).
            resolution = min(max(_num(request.args, "resolution", 96, int), 16), 96)
            try:
                result = reconstruct_from_views(
                    views, resolution=resolution, classification={"label": label},
                    backgrounds=backgrounds, **geom,
                )
            except ValueError:
                pass    # 스캔도 안 되면(교집합 없음 등) 이유가 담긴 CAD 를 그대로 보여준다
            else:
                mesh = result.to_dict()
                mesh["stats"]["cad_fallback"] = reason

    return jsonify({"ok": True, "mesh": mesh})


@bp.post("/scan/model/<object_id>")
def upload_model(object_id: str):
    """기준 CAD 모델(STL/STEP) 업로드. 대시보드의 '기준 모델 등록' 버튼이 쓴다."""
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
        info = save_model(current_hub().cfg.recording.root, object_id, raw)
    except ValueError as exc:
        raise ValidationError(f"STL/STEP 으로 읽을 수 없습니다: {exc}") from exc

    return jsonify({"ok": True, "object_id": object_id, **info})


@bp.post("/scan/reconstruct")
def scan_reconstruct():
    """지정한 물체의 5뷰(object_id)나 저장된 검사 촬영(capture_id)으로 3D 메시를 생성해 반환.

    capture_id(YYYYmmdd_HHMMSS, 같은 초의 촬영은 _02 접미사)가 있으면 object_id 는 무시하고
    inspections/<capture_id>/<cam>.jpg 를 읽어 /api/scan/capture 와 같은 방식으로 다시 만든다 —
    응답에 capture_id·cameras 가 같이 간다. 계산만 하므로 자동 사이클 중에도 막지 않는다.

    응답은 three.js 가 바로 먹는 vertices/faces/vertex_colors.
    큰 메시는 JSON 이 무거우므로 resolution 으로 조절한다.
    """
    body = request.json or {}
    hub = current_hub()
    object_id = body.get("object_id")
    label = body.get("label", "good")
    resolution = min(max(_num(body, "resolution", 48, int), 16), 96)

    capture_id = body.get("capture_id")
    if capture_id is not None:
        if not isinstance(capture_id, str):
            raise ValidationError(f"capture_id 는 문자열이어야 합니다: {capture_id!r}")
        views = _capture_views(hub, capture_id)
        return jsonify({"ok": True, "capture_id": capture_id, "cameras": sorted(views),
                        "mesh": _reconstruct_scan(hub, views, resolution)})

    if not object_id:
        raise ValidationError("object_id 또는 capture_id 가 필요합니다.")

    try:
        views = load_object_views(hub.cfg.recording.root, label, object_id)
    except FileNotFoundError as exc:
        raise ValidationError(str(exc)) from exc

    classification = body.get("classification") or {
        "label": label,
        "confidence": float(body.get("confidence", 0.0)),
    }
    # 외부 검출기(CNN Grad-CAM 등)가 위치를 줄 때만 쓴다. 예전에는 defect 라벨이면 고정 좌표
    # 하나를 칠했는데, 실제 결함 위치와 무관해 '주황이 엉뚱한 곳에 뜬다'의 원인이었다.
    # 주황은 이제 재구성이 실제 촬영 색에서 찾은 표면 이상이다(surface_anomaly).
    defect_regions = body.get("defect_regions")

    result = reconstruct_from_views(
        views,
        resolution=resolution,
        defect_regions=defect_regions,
        classification=classification,
        # 빈 스캔대 참조가 있으면 배경 차분, 없으면 Otsu 폴백
        backgrounds=load_backgrounds(hub.cfg.recording.root),
        # 리그 기하는 물리적 사실이라 코드가 아니라 config 가 갖는다.
        **_scan_geometry(hub),
    )
    return jsonify({"ok": True, "mesh": result.to_dict()})


@bp.post("/scan/reconstruct_demo")
def scan_reconstruct_demo():
    """업로드/스캔 없이 즉시 확인용. 합성 5뷰(구·실루엣)로 재구성.

    카메라·물체 없이도 3D 뷰어와 하이라이트가 동작하는지 검증한다.
    """
    body = request.json or {}
    label = body.get("label", "defect")
    resolution = min(max(_num(body, "resolution", 48, int), 16), 96)

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
        # 신뢰도는 넣지 않는다 — CNN 이 연결돼 있지 않은데 숫자를 보이면 판정 결과처럼 읽힌다.
        classification={"label": label},
    )
    return jsonify({"ok": True, "mesh": result.to_dict()})
