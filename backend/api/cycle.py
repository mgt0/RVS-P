"""자동 사이클 API — 시작 버튼 한 번 = 집기 → 이송 → 검사 → 분류 한 사이클.

진행 상태는 /api/status 의 `cycle` 키로도 폴링한다. 규칙(시작 전 점검·중단·판정)은 core/cycle.py 에 있다.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import current_hub

bp = Blueprint("cycle", __name__, url_prefix="/api/cycle")


def _ok(cycle: dict):
    return jsonify({"ok": True, "cycle": cycle})


@bp.get("/status")
def status():
    return _ok(current_hub().cycle.status())


@bp.post("/start")
def start():
    """준비가 안 됐거나 이미 돌고 있으면 400(이유가 메시지에 담긴다)."""
    return _ok(current_hub().cycle.start())


@bp.post("/stop")
def stop():
    """즉시 중단한다. 벨트와 자율 실행을 세우고 state 는 aborted 가 된다(돌고 있지 않으면 그대로 돌려준다)."""
    return _ok(current_hub().cycle.stop())


@bp.post("/baskets/empty")
def empty_baskets():
    """관리자가 바스켓을 실제로 비운 뒤 개수를 0 으로 되돌린다. body: {"basket": "good" | "defect" | "all"}"""
    body = request.get_json(silent=True)
    basket = body.get("basket") if isinstance(body, dict) else None
    return _ok(current_hub().cycle.empty_baskets(basket))
