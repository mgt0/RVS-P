"""사람이 직접 찍은 기구 값 저장소.

config.yaml 은 손으로 적는 설정이고, 여기 있는 값은 '축을 실제로 움직여 찍은'
측정치다. 둘을 섞지 않는 이유는 두 가지다.

1) 프로그램이 config.yaml 을 다시 쓰면 주석이 통째로 날아간다. 이 프로젝트의
   config.yaml 은 실측 근거와 함정이 주석으로 빼곡해서 그게 본문보다 중요하다.
2) 출처가 다르다. config 값은 '이렇게 하기로 정한 것', 여기 값은 '재보니 이랬다'.

읽기: config 기본값을 두고, 찍은 값이 있으면 그것이 이긴다.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


class Calibration:
    """축 이름 -> {키: 값} 의 얕은 저장소. 파일이 없거나 깨져도 빈 값으로 뜬다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 캘리브레이션이 깨졌다고 서버가 못 뜨면 안 된다. config 기본값으로 간다.
            logger.warning("캘리브레이션 파일을 읽을 수 없습니다: %s", self._path, exc_info=True)
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}

    def get(self, axis: str, key: str, default=None):
        return self._data.get(axis, {}).get(key, default)

    def set(self, axis: str, key: str, value) -> None:
        """값을 저장하고 즉시 파일에 쓴다. 다음 부팅에도 남아야 하기 때문이다."""
        with _lock:
            self._data.setdefault(axis, {})[key] = value
            tmp = self._path.with_suffix(".tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # 임시 파일에 쓰고 교체한다. 쓰는 도중 죽어도 원본이 남는다.
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self._path)
            except OSError:
                logger.exception("캘리브레이션 저장 실패: %s", self._path)
