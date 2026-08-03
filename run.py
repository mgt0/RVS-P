#!/usr/bin/env python3
"""개발용 실행 진입점.

주의: Flask 개발 서버는 MJPEG 스트림 5개 + 폴링을 감당하기 빠듯합니다.
threaded=True 로 띄우되, 실제 데모/수집에는 scripts/serve.sh 의
gunicorn+gevent 구성을 사용하세요.
"""

from backend.app import create_app
from backend.config import load_config


def main() -> None:
    cfg = load_config()
    app = create_app(cfg)
    app.run(
        host=cfg.server.host,
        port=cfg.server.port,
        debug=cfg.server.debug,
        threaded=True,
        # reloader 는 프로세스를 두 번 띄워 카메라를 두 번 연다.
        # 장치가 잠기므로 반드시 끈다.
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
