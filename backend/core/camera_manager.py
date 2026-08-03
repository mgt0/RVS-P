"""5대 카메라 스레드 캡처 + 타임스탬프 동기화.

설계 근거
---------
* 카메라당 전용 스레드: cv2.VideoCapture.read() 는 블로킹이다. 한 루프에서
  5대를 순차로 읽으면 지연이 누적돼 프레임 간 skew 가 100ms 를 넘긴다.
  read() 는 내부에서 GIL 을 놓기 때문에 파이썬 스레드로도 병렬성이 나온다.
* latest-frame-wins: 드라이버 버퍼에 쌓인 오래된 프레임은 텔레오퍼레이션에
  독이다. 큐를 쌓지 않고 항상 최신 1장만 유지한다(CAP_PROP_BUFFERSIZE=1 병행).
* 타임스탬프는 time.monotonic() 기준. wall clock 은 NTP 보정으로 역행할 수 있어
  프레임 정렬에 쓰면 안 된다. 저장 시 epoch 오프셋을 함께 기록해 복원 가능하게 한다.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from ..config import CameraConfig
from ..utils.errors import CameraUnavailableError, UnknownCameraError


@dataclass(frozen=True)
class Frame:
    """캡처된 한 장. image 는 소유권 이전(복사본)이라 호출자가 자유롭게 써도 된다."""

    image: np.ndarray
    timestamp: float        # time.monotonic() 기준 (초)
    index: int              # 카메라별 단조 증가 프레임 번호


class _CameraWorker(threading.Thread):
    """카메라 1대 전담 캡처 스레드."""

    RECONNECT_BACKOFF_SEC = 2.0

    # 링버퍼 길이. 공통 기준시각에서 최대 ±(N/2 / fps) 초까지 되짚을 수 있다.
    # 8프레임 @30fps ≈ ±133ms 로, 카메라 간 위상차를 흡수하기에 충분하다.
    BUFFER_SIZE = 8

    def __init__(self, cfg: CameraConfig, simulate: bool = False) -> None:
        super().__init__(name=f"cam-{cfg.name}", daemon=True)
        self.cfg = cfg
        self._simulate = simulate
        self._cap: cv2.VideoCapture | None = None
        self._latest: Frame | None = None
        self._buffer: deque[Frame] = deque(maxlen=self.BUFFER_SIZE)
        self._lock = threading.Lock()
        self._new_frame = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._frame_index = 0
        self._fps_ema = 0.0
        self._last_ts = 0.0
        self._error: str | None = None

    # ---------- 라이프사이클 ----------
    def _open(self) -> bool:
        if self._simulate:
            self._error = None
            return True
        cap = cv2.VideoCapture(self.cfg.device)
        if not cap.isOpened():
            self._error = f"device {self.cfg.device} open 실패"
            cap.release()
            return False

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        # 지연 누적 방지. 일부 백엔드는 무시하므로 성공 여부를 신뢰하지 않는다.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._cap = cap
        self._error = None
        return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._cap is None and not self._simulate:
                if not self._open():
                    # 실패해도 스레드를 죽이지 않는다. 케이블을 다시 꽂으면 자동 복구.
                    self._stop_event.wait(self.RECONNECT_BACKOFF_SEC)
                    continue

            ok, image = self._read_once()
            if not ok:
                self._release()
                self._stop_event.wait(self.RECONNECT_BACKOFF_SEC)
                continue

            now = time.monotonic()
            self._publish(image, now)

        self._release()

    def _read_once(self) -> tuple[bool, np.ndarray | None]:
        if self._simulate:
            # 하드웨어 없이 파이프라인을 검증하기 위한 합성 프레임.
            # 각 스레드가 독립적으로 sleep 하면 카메라 간 위상이 제각각이라
            # 실기보다 훨씬 큰 skew 가 관측된다. 공통 격자 시각에 정렬해
            # 동기화 지표가 의미 있는 값을 갖게 한다.
            period = 1.0 / max(self.cfg.fps, 1)
            now = time.monotonic()
            time.sleep(max(0.0, (now // period + 1) * period - now))
            return True, self._synthetic_frame()
        assert self._cap is not None
        ok, image = self._cap.read()
        if not ok or image is None:
            self._error = "프레임 read 실패 (장치 분리 또는 대역폭 부족)"
            return False, None
        return True, image

    def _synthetic_frame(self) -> np.ndarray:
        img = np.zeros((self.cfg.height, self.cfg.width, 3), dtype=np.uint8)
        img[:] = (26, 28, 32)  # 대시보드 canvas-soft 와 같은 톤
        t = time.monotonic()
        cx = int((np.sin(t) * 0.5 + 0.5) * (self.cfg.width - 80)) + 40
        cy = self.cfg.height // 2
        cv2.circle(img, (cx, cy), 28, (23, 122, 255), 2)  # BGR 의 #ff7a17
        cv2.putText(img, f"SIM {self.cfg.name.upper()}", (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        return img

    def _publish(self, image: np.ndarray, ts: float) -> None:
        with self._new_frame:
            self._frame_index += 1
            frame = Frame(image=image, timestamp=ts, index=self._frame_index)
            self._latest = frame
            self._buffer.append(frame)
            if self._last_ts:
                inst = 1.0 / max(ts - self._last_ts, 1e-6)
                # EMA(α=0.1): 순간 fps 는 요동이 심해 UI 숫자가 읽히지 않는다.
                self._fps_ema = inst if self._fps_ema == 0 else self._fps_ema * 0.9 + inst * 0.1
            self._last_ts = ts
            self._new_frame.notify_all()

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def stop(self) -> None:
        self._stop_event.set()

    # ---------- 조회 ----------
    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def nearest(self, target_ts: float) -> Frame | None:
        """기준 시각에 가장 가까운 프레임.

        '항상 최신 프레임'을 쓰면 카메라마다 캡처 위상이 달라 최대 한 주기
        (30fps → 33ms)까지 어긋난다. 샘플링 주기와 캡처 주기가 같을 때
        특히 심한데, 두 주기의 경계에서 어떤 카메라는 새 프레임을,
        어떤 카메라는 직전 프레임을 들고 있기 때문이다(엘리어싱).
        공통 기준시각에서 최근접을 고르면 이 위상차가 상쇄된다.
        """
        with self._lock:
            if not self._buffer:
                return None
            return min(self._buffer, key=lambda f: abs(f.timestamp - target_ts))

    def wait_for_new(self, last_index: int, timeout: float = 1.0) -> Frame | None:
        """MJPEG 스트림용. 같은 프레임을 중복 전송하지 않도록 새 프레임을 기다린다."""
        with self._new_frame:
            if self._latest is not None and self._latest.index != last_index:
                return self._latest
            self._new_frame.wait(timeout)
            return self._latest

    @property
    def fps(self) -> float:
        return round(self._fps_ema, 1)

    @property
    def healthy(self) -> bool:
        latest = self.latest()
        return latest is not None and (time.monotonic() - latest.timestamp) < 2.0

    def status(self) -> dict:
        return {
            "name": self.cfg.name,
            "device": str(self.cfg.device),
            "resolution": self.cfg.resolution,
            "target_fps": self.cfg.fps,
            "fps": self.fps,
            "connected": self.healthy,
            "error": None if self.healthy else (self._error or "프레임 수신 없음"),
        }


class CameraManager:
    """카메라 5대를 하나의 인터페이스로 묶는 Facade."""

    def __init__(self, cameras: dict[str, CameraConfig], simulate: bool = False) -> None:
        self._workers = {
            name: _CameraWorker(cfg, simulate=simulate) for name, cfg in cameras.items()
        }
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for worker in self._workers.values():
            worker.start()
        self._started = True

    def stop(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        for worker in self._workers.values():
            worker.join(timeout=2.0)
        self._started = False

    def names(self) -> list[str]:
        return list(self._workers)

    def worker(self, name: str) -> _CameraWorker:
        try:
            return self._workers[name]
        except KeyError as exc:
            # 존재하지 않는 이름은 클라이언트 오류(404)다. 재시도해도 소용없으므로
            # 일시적 장애를 뜻하는 503(CameraUnavailableError)과 구분한다.
            raise UnknownCameraError(
                f"등록되지 않은 카메라 '{name}'. 사용 가능: {', '.join(self._workers)}"
            ) from exc

    def status_all(self) -> list[dict]:
        return [w.status() for w in self._workers.values()]

    def snapshot(self) -> tuple[dict[str, Frame], float]:
        """5대에서 시간적으로 정렬된 한 세트를 뽑고 skew(ms)를 함께 반환.

        2단계로 동작한다:
          1) 각 카메라의 최신 타임스탬프 중 '중앙값'을 공통 기준시각으로 삼는다.
             평균이 아니라 중앙값인 이유는 카메라 한 대가 멈추거나 크게 지연될 때
             기준시각 전체가 끌려가지 않게 하기 위함이다.
          2) 각 카메라 링버퍼에서 기준시각 최근접 프레임을 고른다.

        소프트웨어 동기화의 한계는 남아 있으므로, 남은 skew 를 숨기지 않고
        측정해 metadata 에 기록한다. 학습 단계에서 임계값을 넘는 구간을
        필터링할 수 있어야 하기 때문이다.
        """
        latest = {name: w.latest() for name, w in self._workers.items()}
        available = {n: f for n, f in latest.items() if f is not None}
        if not available:
            return {}, 0.0

        reference_ts = median(f.timestamp for f in available.values())

        frames: dict[str, Frame] = {}
        for name in available:
            frame = self._workers[name].nearest(reference_ts)
            if frame is not None:
                frames[name] = frame

        if not frames:
            return {}, 0.0

        timestamps = [f.timestamp for f in frames.values()]
        skew_ms = (max(timestamps) - min(timestamps)) * 1000.0
        return frames, skew_ms

    def encode_jpeg(self, image: np.ndarray, quality: int = 80) -> bytes:
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise CameraUnavailableError("unknown", "JPEG 인코딩 실패")
        return buf.tobytes()
