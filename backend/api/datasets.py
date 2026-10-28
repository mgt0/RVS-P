"""검사 샘플·물체·데이터 준비도 API.

라벨은 lerobot_data/inspections/<capture_id>/label.json 에만 쓴다(classification/ 은 건드리지 않는다).
이유와 규칙은 core/inspections.py 에 있다. 촬영 자체는 POST /api/scan/capture.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from ..core import inspections
from ..utils.errors import ValidationError
from . import current_hub

bp = Blueprint("datasets", __name__, url_prefix="/api")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _scan():
    """(수집 루트, 카메라 슬롯, 에피소드 메타, 전체 촬영). 요청마다 디스크에서 읽는다 — 캐시하지 않는다."""
    hub = current_hub()
    root = hub.cfg.recording.root
    slots = list(hub.cfg.cameras)
    return root, slots, hub.recorder.index.list(), inspections.all_captures(root, slots)


@bp.get("/inspections")
def list_inspections():
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        raise ValidationError(f"limit 는 정수여야 합니다: {request.args.get('limit')!r}") from None
    limit = min(max(limit, 1), MAX_LIMIT)
    _, _, episodes, captures = _scan()
    return jsonify({
        "ok": True,
        "inspections": captures[:limit],
        "summary": inspections.summary(episodes, captures),    # limit 와 무관하게 전체 기준
    })


@bp.get("/inspections/<capture_id>/<cam>.jpg")
def inspection_image(capture_id: str, cam: str):
    hub = current_hub()
    path = inspections.image_path(hub.cfg.recording.root, list(hub.cfg.cameras), capture_id, cam)
    # recording.root 는 config.yaml 의 './lerobot_data' 처럼 상대 경로일 수 있다. send_file 은 상대 경로를
    # 현재 디렉터리가 아니라 app.root_path(backend/) 에 붙이므로 resolve 하지 않으면 파일을 못 찾는다.
    response = send_file(path.resolve(), mimetype="image/jpeg")
    # 촬영본은 한 번 저장되면 바뀌지 않는다. 사람이 찍힐 수 있어 공유 캐시에는 남기지 않는다(private).
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@bp.post("/inspections/<capture_id>/label")
def label_inspection(capture_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValidationError("요청 본문은 JSON 객체여야 합니다.")
    hub = current_hub()
    entry = inspections.set_label(
        hub.cfg.recording.root, list(hub.cfg.cameras), capture_id,
        object_id=body.get("object_id"),
        label=body.get("label"),
        defect_type=body.get("defect_type"),
        orange_eval=body.get("orange_eval"),
        anomaly=body.get("anomaly"),
    )
    return jsonify({"ok": True, "inspection": entry})


@bp.delete("/inspections/<capture_id>")
def delete_inspection(capture_id: str):
    inspections.delete_capture(current_hub().cfg.recording.root, capture_id)
    return jsonify({"ok": True})


@bp.get("/objects")
def list_objects():
    root, _, episodes, captures = _scan()
    return jsonify({
        "ok": True,
        "recommended_poses": inspections.RECOMMENDED_POSES,
        "objects": inspections.object_list(root, episodes, captures),
    })


@bp.get("/readiness")
def readiness():
    _, _, episodes, captures = _scan()
    return jsonify({"ok": True, **inspections.readiness(episodes, captures)})
