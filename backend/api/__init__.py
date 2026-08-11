"""API 블루프린트 공통.

라우트가 하드웨어에 닿는 통로를 한 곳에 둔다. 이전에는 라우트 모듈마다
같은 `_hub()` 를 각자 정의하고 있었다(5벌).

이름을 `hub` 가 아니라 `current_hub` 로 둔 이유: 라우트 안에서 `hub = ...` 로
받아 쓰는 자리가 많은데, 같은 이름이면 지역 변수가 함수를 가려 두 번째
호출부터 터진다.
"""

from flask import current_app


def current_hub():
    """이 요청이 쓰는 HardwareHub. 앱 팩토리가 app.extensions 에 넣어둔 것."""
    return current_app.extensions["omx_hub"]
