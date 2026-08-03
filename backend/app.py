"""Flask 애플리케이션 팩토리.

Factory 패턴을 쓰는 이유: 테스트에서 simulate=True 설정으로 앱 인스턴스를
여러 개 만들 수 있어야 하고, 전역 `app = Flask(__name__)` 는 임포트 시점에
하드웨어를 붙잡아버려 pytest 수집 단계에서부터 카메라를 연다.
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from .api import analysis as analysis_api
from .api import ports as ports_api
from .api import recording as recording_api
from .api import stream as stream_api
from .config import AppConfig, load_config
from .core.hardware import HardwareHub
from .utils.errors import OmxError

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None) -> Flask:
    cfg = config or load_config()

    logging.basicConfig(
        level=logging.DEBUG if cfg.server.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )

    app = Flask(__name__, static_folder=None)
    app.config["OMX"] = cfg

    hub = HardwareHub(cfg)
    hub.start()
    app.extensions["omx_hub"] = hub

    # Flask 의 teardown 은 요청 단위라 프로세스 종료에 안 걸린다. atexit 이 맞다.
    atexit.register(hub.shutdown)

    app.register_blueprint(ports_api.bp)
    app.register_blueprint(stream_api.bp)
    app.register_blueprint(recording_api.bp)
    app.register_blueprint(analysis_api.bp)

    _register_error_handlers(app)
    _register_frontend(app)

    return app


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(OmxError)
    def handle_domain_error(exc: OmxError):
        # 도메인 예외는 사용자에게 그대로 보여줘도 안전한 메시지만 담고 있다.
        return jsonify(exc.to_dict()), exc.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        return (
            jsonify({"ok": False, "error": {"code": "http_error", "message": exc.description}}),
            exc.code or 500,
        )

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # 예상치 못한 예외의 내부 정보는 로그에만 남기고 클라이언트엔 노출하지 않는다.
        logger.exception("처리되지 않은 예외")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "서버 내부 오류가 발생했습니다. 서버 로그를 확인하세요.",
                    },
                }
            ),
            500,
        )


def _register_frontend(app: Flask) -> None:
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(FRONTEND_DIR / "static", filename)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})
