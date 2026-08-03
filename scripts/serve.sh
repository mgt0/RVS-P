#!/usr/bin/env bash
# 프로덕션 서빙. MJPEG 스트림은 연결당 워커를 붙잡으므로
# 스레드/그린렛 기반 워커가 필수다.
#
# 왜 gevent 인가:
#   sync 워커는 스트림 5개가 워커 5개를 영구 점유해 API 가 굶는다.
#   gevent 는 I/O 대기 중 컨텍스트를 양보하므로 워커 1개로 수십 연결을 받는다.
set -euo pipefail

exec gunicorn \
  --worker-class gevent \
  --workers 1 \
  --worker-connections 64 \
  --timeout 0 \
  --bind "0.0.0.0:${PORT:-5000}" \
  "backend.app:create_app()"
