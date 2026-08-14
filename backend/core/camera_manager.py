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

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from ..config import CameraConfig
from ..utils.errors import CameraUnavailableError, UnknownCameraError

logger = logging.getLogger(__name__)

# 열려 있는 DirectShow 인덱스 대장. USB 브라운아웃 후 재열거가 출렁이는 동안
# 워커들이 서로 다른 시점에 '몇 번째 Arducam' 을 해석하면 같은 인덱스에 두 워커가
# 도달할 수 있다(실측: 프리뷰 5칸에 실물 2대가 겹쳐 표시). 점유를 전역으로 기록해
# 이미 잡힌 장치는 두 번째 워커가 건너뛰고 재시도하게 한다.
_claimed_lock = threading.Lock()
_claimed_indexes: set[int] = set()


def _claim_index(index: int) -> bool:
    with _claimed_lock:
        if index in _claimed_indexes:
            return False
        _claimed_indexes.add(index)
        return True


def _release_index(index: int) -> None:
    with _claimed_lock:
        _claimed_indexes.discard(index)


def init_com() -> None:
    """스레드마다 한 번 COM 을 초기화한다.

    DirectShow 는 COM 위에 올라가 있어서, 초기화하지 않은 스레드에서는
    장치 열거가 CoInitialize 오류로 죽고 OpenCV 의 DSHOW 캡처도
    "backend is generally available but can't be used to capture by index" 로
    실패한다(둘 다 실측). 메인 스레드는 comtypes 임포트가 대신 해주기 때문에
    스크립트에서는 멀쩡하다가 서버의 워커 스레드에서만 터진다.

    같은 스레드에서 여러 번 불러도 안전하다(참조 카운트만 증가).
    """
    try:
        import comtypes
    except ImportError:
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        logger.debug("CoInitialize 실패 — 이미 다른 모드로 초기화된 스레드일 수 있습니다")


def list_device_names() -> list[str]:
    """DirectShow 가 보고하는 카메라 이름 목록(열거 순서 = DSHOW 인덱스).

    pygrabber 가 없거나 Windows 가 아니면 빈 목록 — 호출부가 인덱스 방식으로
    되돌아간다. 이름 매칭이 안 된다고 서버가 못 뜨면 안 된다.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        logger.warning("pygrabber 가 없어 이름 매칭을 쓸 수 없습니다: pip install pygrabber")
        return []
    try:
        return list(FilterGraph().get_input_devices())
    except Exception:  # noqa: BLE001 — COM 초기화 실패 등
        logger.exception("DirectShow 장치 열거 실패")
        return []


def resolve_device(match_name: str, nth: int) -> int | None:
    """이름이 match_name 을 포함하는 장치 중 nth 번째의 DirectShow 인덱스.

    부분 일치를 쓰는 이유: 드라이버가 이름 뒤에 접미사를 붙이는 경우가 있어
    완전 일치는 잘 깨진다. 대소문자는 무시한다.
    """
    needle = match_name.lower()
    matches = [i for i, n in enumerate(list_device_names()) if needle in n.lower()]
    if nth < len(matches):
        return matches[nth]
    return None


@dataclass(frozen=True)
class Frame:
    """캡처된 한 장. image 는 소유권 이전(복사본)이라 호출자가 자유롭게 써도 된다."""

    image: np.ndarray
    timestamp: float        # time.monotonic() 기준 (초)
    index: int              # 카메라별 단조 증가 프레임 번호


class _CameraWorker(threading.Thread):
    """카메라 1대 전담 캡처 스레드."""

    RECONNECT_BACKOFF_SEC = 2.0

    # 이 시간 이상 새 프레임이 없으면 '굳었다'고 본다. 30fps 기준 정상 간격은 33ms 라
    # 3초는 일시적 지연을 오진하지 않으면서 영구 정지는 확실히 잡는 값.
    STALL_SEC = 3.0

    # 자동 WB 가 수렴하도록 기다리는 프레임 수. 15fps 기준 약 4초.
    # 실측(Arducam, 흰 벽): 10프레임에서는 아직 초록이 남고 60프레임이면 중립
    # (B227/G230/R227). 30 은 부족했다. 그 뒤에 잠가야 초록 캐스트 없이 고정된다.
    WB_SETTLE_FRAMES = 60

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
        self._last_ts = 0.0
        self._error: str | None = None
        self._truncated_count = 0   # 대역폭 부족으로 버린 프레임 수(진단용)
        self._wb_locked = True      # _open() 에서 설정에 따라 다시 정한다
        self._wb_lock_at = 0
        self._claimed: int | None = None   # 점유 중인 dshow 인덱스

    # ---------- 라이프사이클 ----------
    def _open(self) -> bool:
        if self._simulate:
            self._error = None
            return True
        # 백엔드 선택은 '무엇으로 카메라를 지목했는가'가 결정한다.
        #
        # match_name 을 쓰면 반드시 CAP_DSHOW 다. 이름의 출처가 DirectShow 열거이고,
        # MSMF 는 열거 순서가 다르기 때문이다(실측: DSHOW 0=ACER 내장캠, MSMF 1=ACER).
        # 이름으로 찾은 인덱스를 MSMF 로 열면 엉뚱한 카메라가 잡힌다.
        #
        # 인덱스로 지목하면 기본값(Windows=MSMF)을 쓴다.
        # (예전 주석은 "DSHOW 가 이 Arducam 들과 MJPG 협상에 실패한다"고 단정했는데
        #  사실이 아니었다. 아래 프로퍼티 설정 순서가 원인이었고, 순서를 고치면
        #  DSHOW 도 MJPG 로 붙는다 — 실측.)
        #
        # params 형태 생성자(VideoCapture(dev, backend, [PROP_..._TIMEOUT_MSEC, ...])) 는
        # 쓰지 않는다. 이 환경에서 5대 전부 isOpened()=False 가 된다(측정).
        # read 타임아웃은 MSMF 가 지원하지 않으므로(cap.set → False, get → -1)
        # 굳은 read 는 CameraManager 의 감시 스레드가 force_reopen() 으로 풀어준다.
        if self.cfg.match_name:
            index = resolve_device(self.cfg.match_name, self.cfg.nth)
            if index is None:
                self._error = (
                    f"'{self.cfg.match_name}' 이름의 카메라 {self.cfg.nth + 1}번째를 찾지 못했습니다. "
                    f"연결된 장치: {', '.join(list_device_names()) or '없음'}"
                )
                return False
            if not _claim_index(index):
                self._error = (
                    f"dshow {index} 는 다른 카메라 슬롯이 사용 중입니다. "
                    "장치 재열거가 끝나면 자동으로 풀립니다."
                )
                return False
            self._claimed = index
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            target = f"{self.cfg.match_name}#{self.cfg.nth} (dshow {index})"
        else:
            cap = cv2.VideoCapture(self.cfg.device)
            target = f"device {self.cfg.device}"

        if not cap.isOpened():
            self._error = f"{target} open 실패"
            cap.release()
            self._release_claim()
            return False

        # FOURCC 는 반드시 '맨 마지막'에 설정한다. 해상도나 FPS 를 FOURCC 뒤에 쓰면
        # DSHOW 가 포맷 협상을 다시 하면서 YUY2(무압축, 640x480@30 = 147Mbps)로
        # 되돌린다. 그러면 USB 2.0(480Mbps)에 1대밖에 못 들어가 나머지는 open 실패.
        #
        # 실측(Arducam 1080P Low Light ×5, 동시 개방):
        #   fourcc → 해상도        : YUY2   1/5 대
        #   해상도 → fourcc → fps  : YUY2   1/5 대
        #   해상도 → fps → fourcc  : MJPG   3/5 대
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        # 지연 누적 방지. 일부 백엔드는 무시하므로 성공 여부를 신뢰하지 않는다.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if self.cfg.exposure is not None:
            # 자동 노출을 먼저 끄지 않으면 수동 값이 곧바로 덮어써진다.
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # 0.25 = manual (MSMF/DSHOW 공통)
            cap.set(cv2.CAP_PROP_EXPOSURE, self.cfg.exposure)
            # 게인은 노출로 번 밝기를 노이즈째로 증폭한다. 수동 노출을 쓸 때는
            # 0 으로 두고 밝기는 brightness 로만 맞춘다.
            cap.set(cv2.CAP_PROP_GAIN, 0)
        else:
            # 자동 노출을 '명시적으로 다시 켠다'. UVC 카메라는 설정을 드라이버에
            # 저장하므로, 과거 노출 튜닝 실험이 남긴 수동 노출 값이 계속 살아남는다 —
            # 실측: Arducam 5대 전부 완전 검은 화면(mean 0.0)이었고, 자동 노출을
            # 켜자마자 정상 복귀했다. (자동 WB 재활성화와 같은 원리)
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)

        if self.cfg.brightness is not None:
            cap.set(cv2.CAP_PROP_BRIGHTNESS, self.cfg.brightness)

        if self.cfg.lock_white_balance and self.cfg.white_balance is not None:
            # 색온도를 명시했으면 처음부터 그 값으로 고정한다.
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            cap.set(cv2.CAP_PROP_WB_TEMPERATURE, self.cfg.white_balance)
        else:
            # 자동 WB 를 '명시적으로 다시 켠다'. UVC 카메라는 설정을 드라이버에
            # 저장하므로, 지난 세션에서 꺼둔 자동 WB 가 다음 실행까지 남는다 —
            # 코드에서 안 끄는 것만으로는 부족하다(실측: 재시작해도 초록 유지).
            cap.set(cv2.CAP_PROP_AUTO_WB, 1)
        # 색온도 미지정이면 여기서 자동 WB 를 끄지 '않는다'. 열자마자 끄면 센서가
        # 수렴할 기회가 없어 기본 보정값(초록 캐스트)에 박제된다 — 실측: 화면 전체가
        # 초록으로 나왔다. 대신 몇 프레임 돌려 수렴시킨 뒤 캡처 루프에서 잠근다.
        self._wb_locked = not self.cfg.lock_white_balance or self.cfg.white_balance is not None
        # 재연결 때도 '이번 open 기준' 프레임 수로 수렴을 기다린다. _frame_index 는
        # 세션 누적값이라 그대로 비교하면 재연결 직후 수렴 전에 잠가버린다.
        self._wb_lock_at = self._frame_index + self.WB_SETTLE_FRAMES

        self._cap = cap
        self._error = None
        return True

    def run(self) -> None:
        if self.cfg.match_name and not self._simulate:
            init_com()   # 이 스레드에서 DirectShow 를 쓸 것이므로 반드시 먼저

        while not self._stop_event.is_set():
            if self._cap is None and not self._simulate:
                if not self._open():
                    # 실패해도 스레드를 죽이지 않는다. 케이블을 다시 꽂으면 자동 복구.
                    self._stop_event.wait(self.RECONNECT_BACKOFF_SEC)
                    continue

            cycle_started = time.monotonic()

            ok, image = self._read_once()
            if not ok:
                self._release()
                self._stop_event.wait(self.RECONNECT_BACKOFF_SEC)
                continue

            if not self._simulate and self._is_truncated(image):
                # 버리고 다음 프레임을 기다린다. 검은 줄이 남은 프레임을 그대로
                # 내보내면 프리뷰도 학습 데이터도 같이 오염된다.
                self._truncated_count += 1
                continue

            # 캡처 직후 한 번만 뒤집는다. 프리뷰에서만 뒤집으면 저장된 학습
            # 데이터는 거꾸로 남아, 눈으로는 멀쩡한데 모델만 이상해진다.
            # 0 = 상하, 1 = 좌우, -1 = 둘 다(180° 회전과 같다).
            if self.cfg.vflip and self.cfg.hflip:
                image = cv2.flip(image, -1)
            elif self.cfg.vflip:
                image = cv2.flip(image, 0)
            elif self.cfg.hflip:
                image = cv2.flip(image, 1)

            now = time.monotonic()
            self._publish(image, now)

            # 자동 WB 가 수렴한 뒤 잠근다(열자마자 끄면 초록 캐스트에 박제된다).
            # 재연결 때도 같은 경로로 다시 수렴→잠금 된다.
            if not self._wb_locked and self._frame_index >= self._wb_lock_at:
                if self._cap is not None:
                    self._cap.set(cv2.CAP_PROP_AUTO_WB, 0)
                self._wb_locked = True
                logger.info("카메라 %s 화이트밸런스 수렴 완료 — 잠금", self.cfg.name)

            # 다음 '공통 격자 시각'까지 쉰다. 페이싱이 없으면 read() 가 즉시 같은
            # 프레임을 돌려주는 드라이버(측정: MSMF + Arducam 다중 연결)에서 루프가
            # 초당 수만 회 돌며 USB·CPU 를 독점해 다른 카메라가 굶는다.
            #
            # 카메라마다 독립적으로 period 만큼 쉬면 read 시점이 제각각 흩어져
            # 카메라 간 skew 가 한 주기 가까이(측정: 15fps 에서 63ms) 벌어진다.
            # 절대 격자에 맞춰 깨우면 5대의 read 가 같은 순간에 모여 skew 가 줄어든다.
            # (시뮬레이션 경로의 합성 프레임도 같은 이유로 같은 방식을 쓴다)
            #
            # 격자 기준은 read 이전 시각(cycle_started)이다. read 이후 시각으로 잡으면
            # read 가 한 주기를 통째로 소비한 경우 방금 지나간 격자점 다음을 기다리게 돼
            # 실효 fps 가 절반으로 떨어진다(측정: 15 → 7.5).
            period = 1.0 / max(self.cfg.fps, 1)
            target = (cycle_started // period + 1) * period
            self._stop_event.wait(max(0.0, target - time.monotonic()))

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

    def _is_truncated(self, image: np.ndarray) -> bool:
        """잘려서 도착한 MJPEG 프레임인지 판정한다.

        USB 대역폭이 모자라면 프레임이 중간에 끊긴 채 들어오고, OpenCV 는 그걸
        예외 없이 디코딩해 남은 아래쪽을 단색(주로 검정/회색)으로 채운다.
        화면에서는 '검은 줄'로 보이고, 그대로 저장하면 학습 데이터에 섞인다.

        판정: 아래 8줄이 '완전히' 균일한가. 실제 장면은 센서 노이즈 때문에
        픽셀 값이 미세하게라도 흔들리므로 완전 균일은 사실상 나오지 않는다.
        (임계값 대신 완전 일치를 보는 이유 = 정상 프레임을 버리지 않기 위해)
        """
        band = image[-8:]
        return bool(band.size) and bool((band == band.flat[0]).all())

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
            self._last_ts = ts
            self._new_frame.notify_all()

    def _release_claim(self) -> None:
        if self._claimed is not None:
            _release_index(self._claimed)
            self._claimed = None

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._release_claim()

    def stop(self) -> None:
        self._stop_event.set()

    def stalled(self) -> bool:
        """새 프레임이 STALL_SEC 이상 없으면 True. _last_ts=0 은 아직 첫 프레임 전이라 제외."""
        if self._simulate or self._cap is None or not self._last_ts:
            return False
        return (time.monotonic() - self._last_ts) > self.STALL_SEC

    def force_reopen(self) -> None:
        """감시 스레드에서 호출. 굳은 cap 을 닫아 블로킹된 read() 를 풀어준다.

        MSMF 의 read() 에는 타임아웃이 없어서, 카메라가 멈추면 캡처 스레드가 read()
        안에서 영구히 멈춘다(자기 자신은 아무것도 못 한다). 밖에서 cap 을 닫아주면
        read() 가 False 로 빠져나오고 run() 의 기존 재연결 경로가 복구를 이어받는다.
        """
        cap, self._cap = self._cap, None
        self._last_ts = 0.0     # 재오픈 직후 다시 stalled 로 오진되지 않게 초기화
        if cap is not None:
            cap.release()
        self._release_claim()

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
        """링버퍼에 실제로 쌓인 프레임 간격으로 계산한다.

        이전 구현은 `1/max(ts - last_ts, 1e-6)` 의 EMA 였는데, 간격이 0 에 가까운
        프레임이 한 번 끼면 순간값이 100만까지 튀어 EMA 가 수만으로 오염됐다.
        (실측 26fps 인 카메라가 80000, 136475 로 표시돼 진단을 계속 오도했다)
        버퍼 구간 평균은 그런 폭주가 원리적으로 불가능하다.
        """
        with self._lock:
            if len(self._buffer) < 2:
                return 0.0
            span = self._buffer[-1].timestamp - self._buffer[0].timestamp
            if span <= 0:
                return 0.0
            return round((len(self._buffer) - 1) / span, 1)

    @property
    def healthy(self) -> bool:
        latest = self.latest()
        return latest is not None and (time.monotonic() - latest.timestamp) < 2.0

    def status(self) -> dict:
        return {
            "name": self.cfg.name,
            # 화면 표시용 이름. 슬롯 키는 데이터 경로라 그대로 두고 label 만 보여준다.
            "label": self.cfg.label or self.cfg.name,
            "device": f"{self.cfg.match_name}#{self.cfg.nth}" if self.cfg.match_name
                      else str(self.cfg.device),
            "resolution": self.cfg.resolution,
            "target_fps": self.cfg.fps,
            "fps": self.fps,
            # 0 이 아니면 대역폭이 모자란다는 뜻 — 해상도/fps 를 낮추거나 USB 를 분산할 것
            "truncated": self._truncated_count,
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
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    def start(self) -> None:
        if self._started:
            return
        for worker in self._workers.values():
            worker.start()
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watch_loop, name="cam-watchdog", daemon=True)
        self._watchdog.start()
        self._started = True

    def _watch_loop(self) -> None:
        """굳은 카메라를 찾아 강제 재연결시킨다.

        카메라 한 대가 read() 안에서 멈추면 그 스레드는 스스로 빠져나올 수 없으므로
        (MSMF 에 read 타임아웃이 없다) 밖에서 깨워줄 주체가 반드시 하나 필요하다.
        이게 없으면 멈춘 카메라가 영구히 죽은 채 남는다.
        """
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(1.0)
            for worker in self._workers.values():
                if worker.stalled():
                    logger.warning("카메라 %s 정지 감지 — 강제 재연결", worker.cfg.name)
                    worker.force_reopen()

    def stop(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog:
            self._watchdog.join(timeout=2.0)
            self._watchdog = None
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
