"""시연 녹화 세션 API — 녹화 → 리셋 → 다음 에피소드를 서버가 반복한다.

진행 상태는 /api/status 의 `demo` 키로 폴링한다. 규칙(가드·범위·종료 조건)은 DemoSession 에 있다.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import current_hub

bp = Blueprint("demo", __name__, url_prefix="/api/demo")


@bp.post("/start")
def start():
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    demo = current_hub().demo.start(
        object_id=body.get("object_id", ""),
        task=body.get("task", ""),
        episode_sec=body.get("episode_sec", 20),
        reset_sec=body.get("reset_sec", 10),
    )
    return jsonify({"ok": True, "demo": demo})


@bp.post("/next")
def next_episode():
    """저장하고 다음. 녹화 중이면 지금 저장하고 리셋으로, 리셋 중이면 리셋을 건너뛴다."""
    return jsonify({"ok": True, "demo": current_hub().demo.next()})


@bp.post("/redo")
def redo_episode():
    """녹화 중인 에피소드를 버리고, 리셋 뒤 같은 ID 로 다시 녹화한다. 리셋 중에는 409."""
    return jsonify({"ok": True, "demo": current_hub().demo.redo()})


@bp.post("/end")
def end_session():
    """녹화 중이면 지금 에피소드를 저장하고 세션을 끝낸다."""
    return jsonify({"ok": True, "demo": current_hub().demo.end()})
