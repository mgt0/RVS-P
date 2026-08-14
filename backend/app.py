"""Flask 애플리케이션 팩토리.

Factory 패턴을 쓰는 이유: 테스트에서 simulate=True 설정으로 앱 인스턴스를
여러 개 만들 수 있어야 하고, 전역 `app = Flask(__name__)` 는 임포트 시점에
하드웨어를 붙잡아버려 pytest 수집 단계에서부터 카메라를 연다.
"""

from __future__ import annotations

import atexit
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from .api import analysis as analysis_api
from .api import conveyor as conveyor_api
from .api import ports as ports_api
from .api import recording as recording_api
from .api import stream as stream_api
from .config import AppConfig, load_config
from .core.hardware import HardwareHub
from .utils.errors import OmxError

PROJECT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
# 서버를 누가 띄웠든 로그는 항상 이 파일에도 남는다. 터미널 스크롤백은 그 창을
# 띄운 사람만 볼 수 있어서, 문제가 생겼을 때 로그를 손으로 옮겨 붙여야 했다.
LOG_PATH = PROJECT_DIR / "logs" / "server.log"

logger = logging.getLogger(__name__)


LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def enable_file_logging() -> Path | None:
    """로그를 logs/server.log 에도 남긴다. 5MB 씩 3개까지 돌려 쓴다.

    실제로 서버를 띄우는 run.py 에서만 부른다. create_app() 에서 부르면 테스트가
    돌 때마다 pytest 로그가 같은 파일에 섞여 정작 필요할 때 읽기 어려워진다.

    터미널 스크롤백은 그 창을 띄운 사람만 볼 수 있다. 파일로 남겨두면 서버를
    누가 띄웠든 로그를 같이 볼 수 있다(scripts/server_status.py).
    """
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return LOG_PATH
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        return LOG_PATH
    except OSError:
        # 로그 파일을 못 열어도 서버는 떠야 한다(권한/디스크 문제).
        logger.warning("로그 파일을 열 수 없습니다: %s", LOG_PATH, exc_info=True)
        return None


def create_app(config: AppConfig | None = None) -> Flask:
    cfg = config or load_config()

    logging.basicConfig(
        level=logging.DEBUG if cfg.server.debug else logging.INFO,
        format=LOG_FORMAT,
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
    app.register_blueprint(conveyor_api.bp)

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
    # 프런트엔드는 빌드 스텝이 없어 파일을 고치면 그게 곧 배포본이다. 브라우저가
    # 캐시를 들고 있으면 고친 코드가 반영되지 않고, 이미 고친 버그가 계속 재현돼
    # 엉뚱한 곳을 파게 된다(실측으로 그랬다).
    #
    # no-store 헤더만으로는 부족하다. '이미 저장된' 캐시는 헤더를 바꿔도 만료될
    # 때까지 그대로 쓰이고(Flask 기본 12시간), 그동안 브라우저는 서버에 물어보지도
    # 않는다. 그래서 URL 자체를 바꾼다 — index.html 의 정적 파일 참조에 그 파일의
    # 수정 시각을 붙이면, 파일을 고치는 순간 다른 URL 이 되어 반드시 새로 받는다.
    def _no_cache(response):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    static_ref = re.compile(r"(/static/[\w./-]+\.(?:js|css))")

    def _stamped_index() -> str:
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")

        def stamp(match: re.Match) -> str:
            rel = match.group(1)
            try:
                mtime = int((FRONTEND_DIR / rel.lstrip("/")).stat().st_mtime)
            except OSError:
                return rel          # 없는 파일이면 건드리지 않는다(404 는 그대로 404)
            return f"{rel}?v={mtime}"

        return static_ref.sub(stamp, html)

    @app.get("/")
    def index():
        return _no_cache(app.response_class(_stamped_index(), mimetype="text/html"))

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return _no_cache(send_from_directory(FRONTEND_DIR / "static", filename))

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})
