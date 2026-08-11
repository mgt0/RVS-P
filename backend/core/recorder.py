"""에피소드 수집 오케스트레이션.

패턴
----
* State: IDLE → RECORDING → SAVING → IDLE. 상태 전이를 한 곳에서 검증해
  "STOP 두 번 눌러 두 번 저장" 같은 경쟁 조건을 원천 차단한다.
* Producer/Consumer: 샘플러 스레드는 정확한 주기로 관측치를 큐에 넣고,
  라이터 스레드가 디스크에 쓴다. cv2.imwrite 는 수 ms~수십 ms 씩 걸리므로
  같은 스레드에서 하면 샘플링 주기가 무너져 ACT 학습 데이터의 시간축이 왜곡된다.

백프레셔 정책: 큐가 가득 차면 프레임을 '드롭하지 않고' 샘플링을 늦춘다.
드롭은 조용한 데이터 손실이고, 지연은 metadata 에 기록되어 감지 가능하기 때문.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from enum import Enum

from ..config import RecordingConfig
from ..utils.errors import RecordingStateError
from .arm_controller import ArmController
from .camera_manager import CameraManager
from .storage import (
    ClassificationWriter,
    DatasetIndex,
    DatasetWriter,
    EpisodeMeta,
    EpisodeWriter,
    Sample,
    validate_id,
    validate_label,
)

logger = logging.getLogger(__name__)

_SENTINEL = object()


class RecordingState(str, Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    SAVING = "SAVING"


class Recorder:
    def __init__(
        self,
        cfg: RecordingConfig,
        cameras: CameraManager,
        leader: ArmController,
        follower: ArmController,
    ) -> None:
        self._cfg = cfg
        self._cameras = cameras
        self._leader = leader
        self._follower = follower

        self._state = RecordingState.IDLE
        self._lock = threading.RLock()

        self._queue: queue.Queue = queue.Queue(maxsize=cfg.queue_maxsize)
        self._sampler: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._writers: list[DatasetWriter] = []
        self._meta: EpisodeMeta | None = None
        self._started_at = 0.0
        self._frame_count = 0
        self._max_skew_ms = 0.0
        self._skew_violations = 0
        self._last_error: str | None = None

        self.index = DatasetIndex(cfg.root)
        cfg.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 상태
    @property
    def state(self) -> RecordingState:
        with self._lock:
            return self._state

    def status(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._started_at if self._state == RecordingState.RECORDING else 0.0
            return {
                "state": self._state.value,
                "episode_id": self._meta.episode_id if self._meta else None,
                "object_id": self._meta.object_id if self._meta else None,
                "label": self._meta.label if self._meta else None,
                "defect_type": self._meta.defect_type if self._meta else None,
                "elapsed_sec": round(elapsed, 1),
                "frame_count": self._frame_count,
                "max_skew_ms": round(self._max_skew_ms, 1),
                "skew_violations": self._skew_violations,
                "queue_depth": self._queue.qsize(),
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------ 제어
    def start(self, episode_id: str, object_id: str, label: str, defect_type: str | None) -> dict:
        label, defect_type = validate_label(label, defect_type)
        episode_id = validate_id(episode_id, "episode_id")
        object_id = validate_id(object_id, "object_id")

        with self._lock:
            if self._state != RecordingState.IDLE:
                raise RecordingStateError(
                    f"현재 상태가 {self._state.value} 입니다. IDLE 일 때만 시작할 수 있습니다."
                )

            if not any(w.healthy for w in self._camera_workers()):
                raise RecordingStateError(
                    "정상 수신 중인 카메라가 없습니다. PORT MONITORING 에서 연결을 먼저 확인하세요."
                )

            meta = EpisodeMeta(
                episode_id=episode_id,
                object_id=object_id,
                label=label,
                defect_type=defect_type,
                fps=self._cfg.sample_rate_hz,
                cameras=self._cameras.names(),
                joint_names=[f"joint_{i}" for i in range(len(self._leader.cfg.joint_ids))],
                started_at_epoch=time.time(),
                # monotonic 기준 시각을 epoch 로 되돌리기 위한 오프셋
                monotonic_offset=time.time() - time.monotonic(),
            )

            writers: list[DatasetWriter] = [
                EpisodeWriter(self._cfg.root, jpeg_quality=self._cfg.jpeg_quality),
                ClassificationWriter(self._cfg.root, jpeg_quality=95),
            ]
            for writer in writers:
                writer.open(meta)   # 실패 시(중복 ID 등) 여기서 예외 → 상태는 IDLE 유지

            self._writers = writers
            self._meta = meta
            self._frame_count = 0
            self._max_skew_ms = 0.0
            self._skew_violations = 0
            self._last_error = None
            self._started_at = time.monotonic()
            self._stop_event.clear()
            self._queue = queue.Queue(maxsize=self._cfg.queue_maxsize)

            self._writer_thread = threading.Thread(target=self._writer_loop, name="rec-writer", daemon=True)
            self._sampler = threading.Thread(target=self._sampler_loop, name="rec-sampler", daemon=True)
            self._writer_thread.start()
            self._sampler.start()

            self._state = RecordingState.RECORDING
            logger.info("녹화 시작: episode=%s object=%s label=%s", episode_id, object_id, label)
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            if self._state != RecordingState.RECORDING:
                raise RecordingStateError(
                    f"현재 상태가 {self._state.value} 입니다. 녹화 중일 때만 정지할 수 있습니다."
                )
            self._state = RecordingState.SAVING
            self._stop_event.set()

        # 락 밖에서 조인한다. 라이터가 status() 를 호출할 수 있어 데드락 위험이 있기 때문.
        if self._sampler:
            self._sampler.join(timeout=5.0)
        self._queue.put(_SENTINEL)
        if self._writer_thread:
            self._writer_thread.join(timeout=30.0)

        with self._lock:
            stats = {
                "duration_sec": round(time.monotonic() - self._started_at, 2),
                "max_sync_skew_ms": round(self._max_skew_ms, 2),
                "sync_skew_violations": self._skew_violations,
                "sync_skew_threshold_ms": self._cfg.max_sync_skew_ms,
            }
            try:
                for writer in self._writers:
                    writer.close(self._meta, stats)
            finally:
                self._writers = []
                self._state = RecordingState.IDLE

            logger.info("녹화 종료: %s (%d frames)", self._meta.episode_id, self._frame_count)
            return self.status()

    def abort(self, reason: str) -> None:
        """에러 경로. 부분 저장된 데이터를 남기지 않는다."""
        with self._lock:
            if self._state == RecordingState.IDLE:
                return
            self._stop_event.set()
            for writer in self._writers:
                writer.abort()
            self._writers = []
            self._last_error = reason
            self._state = RecordingState.IDLE
            logger.error("녹화 중단: %s", reason)

    # ------------------------------------------------------------------ 루프
    def _camera_workers(self):
        return [self._cameras.worker(name) for name in self._cameras.names()]

    def _sampler_loop(self) -> None:
        period = 1.0 / self._cfg.sample_rate_hz
        next_tick = time.monotonic()

        try:
            while not self._stop_event.is_set():
                frames, skew_ms = self._cameras.snapshot()
                if frames:
                    sample = Sample(
                        t=time.monotonic() - self._started_at,
                        # 라이터 스레드가 쓰는 동안 캡처 스레드가 같은 버퍼를 덮어쓰지
                        # 않도록 복사한다. 이 복사를 빼면 프레임이 찢긴다.
                        frames={k: v.image.copy() for k, v in frames.items()},
                        frame_timestamps={k: v.timestamp for k, v in frames.items()},
                        leader_joints=self._leader.read(),
                        follower_joints=self._follower.read(),
                        sync_skew_ms=skew_ms,
                    )

                    if skew_ms > self._cfg.max_sync_skew_ms:
                        self._skew_violations += 1
                    self._max_skew_ms = max(self._max_skew_ms, skew_ms)

                    # 큐가 가득 차면 여기서 블로킹된다 = 자연스러운 백프레셔.
                    self._queue.put(sample)

                next_tick += period
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    self._stop_event.wait(sleep_for)
                else:
                    # 주기를 놓쳤다. 누적 드리프트를 막기 위해 기준점을 현재로 리셋.
                    next_tick = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            logger.exception("샘플러 루프 실패")
            self.abort(f"샘플링 실패: {exc}")

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                break
            try:
                for writer in self._writers:
                    writer.write(item)
                self._frame_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("라이터 루프 실패")
                self.abort(f"디스크 쓰기 실패: {exc}")
                break
            finally:
                self._queue.task_done()
