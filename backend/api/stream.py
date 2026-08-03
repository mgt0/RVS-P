"""카메라 프레임 전송 API.

MJPEG(multipart/x-mixed-replace)를 고른 이유
-------------------------------------------
* <img src> 하나로 끝난다. WebRTC 대비 구현/의존성이 압도적으로 가볍고,
  로컬 네트워크 데모에서 지연도 충분히 낮다.
* 대안 비교: WebSocket + base64 는 전송량이 33% 늘고 디코딩을 JS 가 떠안는다.
  HLS 는 세그먼트 단위라 최소 지연이 초 단위다.

주의: HTTP/1.1 은 호스트당 동시 연결이 6개로 제한된다(MDN). 카메라 5대 스트림이
5개를 점유하면 폴링 API 가 굶는다. 그래서 스트림에 `?fps=` 파라미터로 상한을
두고, 프론트는 녹화 중이 아닐 때 프리뷰 fps 를 낮춘다.
"""

from __future__ import annotations

import time

from flask import Blueprint, Response, current_app, jsonify, request

from ..utils.errors import CameraUnavailableError

bp = Blueprint("stream", __name__, url_prefix="/api")

BOUNDARY = "omxframe"


def _hub():
    return current_app.extensions["omx_hub"]


@bp.get("/cameras")
def list_cameras():
    return jsonify({"ok": True, "cameras": _hub().cameras.status_all()})


@bp.get("/stream/<name>")
def stream(name: str):
    hub = _hub()
    worker = hub.cameras.worker(name)          # 없는 이름이면 여기서 404
    max_fps = min(max(int(request.args.get("fps", 15)), 1), 30)
    quality = min(max(int(request.args.get("q", 75)), 30), 95)

    def generate():
        last_index = -1
        min_interval = 1.0 / max_fps
        last_sent = 0.0
        while True:
            frame = worker.wait_for_new(last_index, timeout=1.0)
            if frame is None:
                # 장치가 죽어도 연결을 끊지 않는다. 복구되면 자동으로 재개된다.
                time.sleep(0.2)
                continue

            now = time.monotonic()
            if now - last_sent < min_interval:
                time.sleep(min_interval - (now - last_sent))

            last_index = frame.index
            last_sent = time.monotonic()
            payload = hub.cameras.encode_jpeg(frame.image, quality=quality)
            yield (
                b"--" + BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                + payload + b"\r\n"
            )

    return Response(
        generate(),
        mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.get("/snapshot/<name>")
def snapshot(name: str):
    """단일 JPEG. 스트림 연결 수를 아끼고 싶을 때 폴링용으로 쓸 수 있다."""
    hub = _hub()
    worker = hub.cameras.worker(name)
    frame = worker.latest()
    if frame is None:
        raise CameraUnavailableError(name, f"카메라 '{name}' 에서 아직 프레임을 받지 못했습니다.")
    payload = hub.cameras.encode_jpeg(frame.image, quality=85)
    return Response(payload, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
