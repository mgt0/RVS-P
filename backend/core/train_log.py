"""lerobot-train 로그 파서 + 실행 기록(run 폴더) 읽기. 표준 라이브러리만 쓴다.

실제 ACT 학습은 서버 밖 CLI 에서 돈다(act_trainer.LeRobotTrainer 참고). 그 학습을
감싸는 래퍼(scripts/train_act.py)가 아래 폴더에 기록을 남기고, 서버는 같은 폴더를
따라 읽어 차트를 그린다. 그래서 파일 이름·스키마는 이 모듈이 두 쪽의 단일 출처다.

    logs/train/<YYYYmmdd_HHMMSS>/      같은 초에 또 뜬 실행은 _02, _03 … 이 붙는다(두 자리라 사전순 = 시간순)
        run.json       {"status": RUNNING|DONE|FAILED, "returncode", "log_freq", "steps",
                        "precision", "num_workers", "started_at", "finished_at",
                        "output_dir", "args", "cmd"}
        heartbeat      래퍼가 주기적으로 수정 시각만 갱신하는 빈 파일(HEARTBEAT_*). RUNNING 인데 낡으면 read_run 이 FAILED(stale)로 본다
        train.log      lerobot-train 의 stdout+stderr 원본
        metrics.jsonl  파싱한 기록 한 줄씩
                       {"kind": "train", "step": 200, "loss": 5.1, "lr": 1e-5, ...}
                       {"kind": "eval",  "step": 1000, "eval_loss": 0.12}
        gpu.jsonl      nvidia-smi 로 잰 GPU 상태 한 줄씩 (nvidia-smi 가 없으면 파일도 없다)
                       {"ts": 1793145696.3, "util": 97.0, "power": 98.4, ...}  키는 GPU_FIELDS

lerobot(>=0.5) 로그 형식은 lerobot 소스 기준이다. 버전에 따라 미터가 일부 없을 수 있어
'key:value' 토큰을 일반적으로 파싱하고, 어떤 미터가 있는지 가정하지 않는다.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

# 래퍼가 쓰고 서버가 읽는 로그 루트. 이 경로가 두 쪽에서 어긋나면 대시보드가 빈 화면이 된다.
TRAIN_LOG_ROOT = Path(__file__).resolve().parents[2] / "logs" / "train"
RUN_FILE = "run.json"
LOG_FILE = "train.log"
METRICS_FILE = "metrics.jsonl"
GPU_FILE = "gpu.jsonl"
HEARTBEAT_FILE = "heartbeat"

# 래퍼 생존 신호. 래퍼가 HEARTBEAT_INTERVAL_SEC 마다 HEARTBEAT_FILE 의 수정 시각을 갱신하고, read_run 은
# RUNNING 인데 HEARTBEAT_STALE_SEC 넘게 안 갱신됐으면 래퍼가 강제 종료된 것으로 본다.
HEARTBEAT_INTERVAL_SEC = 10
HEARTBEAT_STALE_SEC = 60

# GPU 계측. 래퍼가 GPU_INTERVAL_SEC 마다 nvidia-smi 를 불러 gpu.jsonl 에 남기고, 서버가 최근
# 샘플을 요약해 대시보드 'GPU' 카드로 보낸다. 학습이 느린 원인(데이터 로딩 병목, 전력·열 제한)을
# 화면에서 바로 보려는 것이다. torch 가 아니라 nvidia-smi 하위 프로세스로만 보는 이유: 래퍼는
# torch 가 필요 없고, Windows 스마트 앱 컨트롤이 torch DLL 을 막는 PC 가 있다.
GPU_INTERVAL_SEC = 5
GPU_WINDOW = 6                                  # 요약에 쓰는 최근 샘플 수 (약 30초)
GPU_STALE_SEC = GPU_WINDOW * GPU_INTERVAL_SEC   # 마지막 샘플이 이보다 낡으면 계측이 멈춘 것으로 본다
# (nvidia-smi --query-gpu 필드, gpu.jsonl 키). 순서가 CSV 열 순서다. 마지막 셋은 클럭 제한 사유라
# 값이 "Active"/"Not Active" 다. 전력 한계는 power.limit 이 아니라 enforced.power.limit 을 쓴다 —
# 노트북 GPU(RTX 5070 Laptop 실측)에서 power.limit 은 [N/A] 이고, 지금 실제로 걸려 있는 상한은 후자다.
GPU_FIELDS = (
    ("utilization.gpu", "util"),                                 # %
    ("memory.used", "mem_used"),                                 # MiB
    ("memory.total", "mem_total"),                               # MiB
    ("power.draw", "power"),                                     # W
    ("enforced.power.limit", "power_limit"),                     # W
    ("temperature.gpu", "temp"),                                 # °C
    ("clocks.sm", "sm_clock"),                                   # MHz
    # 구 이름을 쓴다: 신 이름(clocks_event_reasons.*)을 모르는 드라이버는 질의 전체를 거부하고, 새 드라이버는 구 이름도 받는다
    ("clocks_throttle_reasons.sw_power_cap", "sw_power_cap"),
    ("clocks_throttle_reasons.sw_thermal_slowdown", "sw_thermal"),
    ("clocks_throttle_reasons.hw_thermal_slowdown", "hw_thermal"),
)
GPU_QUERY = ",".join(field for field, _ in GPU_FIELDS)
_GPU_FLAGS = {"sw_power_cap", "sw_thermal", "hw_thermal"}
_GPU_TAIL_BYTES = 4096   # read_gpu 가 파일 끝에서 읽는 크기. 샘플 한 줄이 약 220바이트라 창(6개)의 세 배 남짓

# lerobot format_big_number 의 접미사 (1000 단위)
_SUFFIX = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "Q": 1e15}
_NUMBER = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([KMBTQ]?)")
# 평가 줄: "step 1000: eval_loss=0.1234"
_EVAL = re.compile(r"\bstep\s+(\d+):\s*eval_loss=(\S+)")


def parse_number(text: str) -> float | None:
    """'0.123', '1.0e-05', '25K', '1.5M' -> float. 숫자가 아니거나 nan/inf 면 None.

    nan/inf 를 걸러내는 이유: metrics.jsonl 을 엄격한 JSON 으로 유지하고, 차트에
    그릴 수 없는 값이 서버 응답(JSON)에 섞여 브라우저 파싱이 깨지는 걸 막는다.
    """
    m = _NUMBER.fullmatch(text)
    if not m:
        return None
    value = float(m.group(1)) * _SUFFIX[m.group(2)]
    return value if math.isfinite(value) else None


def parse_line(line: str) -> dict | None:
    """로그 한 줄 -> 지표 줄/평가 줄 dict, 그 외 None.

    지표 줄: {"kind": "train", "step_approx": 1000.0, "loss": 0.5, "lr": 1e-05, ...}
      step_approx 는 '1K' 처럼 손실 압축된 값이다(스텝 복원은 TrainLogReader).
    평가 줄: {"kind": "eval", "step": 1000, "eval_loss": 0.1234}
    """
    m = _EVAL.search(line)
    if m:
        loss = parse_number(m.group(2))
        return None if loss is None else {"kind": "eval", "step": int(m.group(1)), "eval_loss": loss}

    tokens = line.split()
    # 메시지는 'step:' 토큰부터다. 앞쪽 로거 접두어(레벨·시각·'lerobot_train.py:412' 같은
    # 위치 필드)에도 콜론이 있어서, 줄 전체를 key:value 로 읽으면 위치 필드가 지표로 새어든다.
    start = next((i for i, t in enumerate(tokens) if t.startswith("step:")), None)
    if start is None:
        return None
    rec: dict = {"kind": "train"}
    for tok in tokens[start:]:
        key, sep, val = tok.partition(":")
        num = parse_number(val) if sep else None
        if num is not None:
            rec["step_approx" if key == "step" else key] = num
    return rec if "step_approx" in rec else None


class TrainLogReader:
    """로그를 한 줄씩 먹여 기록(dict)을 뽑는다. 스텝 복원 상태를 여기서 든다.

    step 이 format_big_number 로 찍혀 1000/1200/1400 이 모두 '1K' 가 되므로 파싱한
    값으로는 정확한 스텝을 알 수 없다. 대신 지표 줄은 log_freq 스텝마다 정확히 한 줄이라
    'n 번째 지표 줄 = n × log_freq 스텝'이다. 파싱한 근사값은 log_freq 를 모를 때만 쓴다.

    한계: --resume 으로 이어받은 실행은 체크포인트 스텝만큼 오프셋이 있는데 알 방법이
    없어서, 복원 스텝이 로그 시작을 0 으로 센다(곡선 모양은 그대로, x 값만 어긋남).
    """

    def __init__(self, log_freq: int | None = None) -> None:
        self.log_freq = log_freq
        self._n_train = 0   # 지금까지 센 지표 줄 수 (loss 가 nan 이라 빠진 줄도 센다)

    def feed(self, line: str) -> dict | None:
        rec = parse_line(line)
        if rec is None or rec["kind"] != "train":
            return rec
        self._n_train += 1
        approx = int(round(rec.pop("step_approx")))
        rec["step"] = self._n_train * self.log_freq if self.log_freq else approx
        return rec


def parse_gpu_line(text: str) -> dict | None:
    """`nvidia-smi --query-gpu=<GPU_QUERY> --format=csv,noheader,nounits` 출력 -> 샘플 dict.

    수치는 float, 클럭 제한 사유는 Active -> True / Not Active -> False 다. 드라이버가 지원하지
    않는 항목('[N/A]' 등)은 None. GPU 가 여럿이면 GPU 마다 한 줄씩 나오는데 첫 줄(GPU 0)만 읽는다.
    열 수가 안 맞거나 비어 있으면 None.
    """
    lines = text.strip().splitlines()
    cols = [c.strip() for c in lines[0].split(",")] if lines else []
    if len(cols) != len(GPU_FIELDS):
        return None
    return {
        key: {"Active": True, "Not Active": False}.get(col) if key in _GPU_FLAGS else parse_number(col)
        for (_, key), col in zip(GPU_FIELDS, cols)
    }


# ------------------------------------------------------------ run 폴더 읽기 (서버 쪽)
def read_run(run_dir: Path) -> dict | None:
    """run.json. 래퍼가 쓰는 도중이라 반쯤 읽히면 None — 호출자는 다음 폴링에 다시 읽는다.

    RUNNING 인데 heartbeat 파일이 있고 HEARTBEAT_STALE_SEC 넘게 안 갱신됐으면 래퍼가 강제 종료된 것으로
    보고 status 를 FAILED 로 바꾸고 "stale": True 를 붙인다(heartbeat 가 없는 옛 실행은 그대로 둔다).
    프로세스 생존은 보지 않는다 — Windows 에서 os.kill(pid, 0) 은 CTRL_C_EVENT 를 보내 학습을 건드린다.
    한계: 노트북 절전에서 깨어난 직후엔 래퍼가 다시 뛰기 전까지 잠깐 낡음으로 보일 수 있다.
    """
    try:
        data = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") == "RUNNING":
        try:
            stale = time.time() - (run_dir / HEARTBEAT_FILE).stat().st_mtime > HEARTBEAT_STALE_SEC
        except OSError:   # heartbeat 가 없는 옛 실행
            stale = False
        if stale:
            data.update(status="FAILED", stale=True)
    return data


def latest_run_dir(root: Path) -> Path | None:
    """run.json 이 있는 가장 최근 실행 폴더. 폴더 이름이 시각이라 사전순 = 시간순."""
    if not root.is_dir():
        return None
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        if (d / RUN_FILE).is_file():
            return d
    return None


def read_new_records(path: Path, pos: int) -> tuple[list[dict], int]:
    """metrics.jsonl 에서 byte 위치 pos 이후의 새 기록과 다음 위치를 돌려준다.

    개행으로 끝난 줄만 소비한다 — 래퍼가 쓰는 중인 마지막 줄을 반쯤 읽고 위치를
    넘겨버리면 그 기록이 영영 유실된다. 깨진 줄은 건너뛴다.
    """
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:   # 아직 파일이 없다
        return [], pos
    end = data.rfind(b"\n") + 1
    records = []
    for raw in data[:end].splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, pos + end


def read_gpu(run_dir: Path) -> dict | None:
    """gpu.jsonl 의 최근 GPU_WINDOW 개 샘플 요약. 샘플이 없거나 마지막이 낡았으면 None(= 카드 숨김).

        {"latest": 마지막 샘플, "util_avg": 평균 사용률(%), "power_cap": 전력 제한 걸림,
         "thermal": 열 제한 걸림, "samples": 요약에 쓴 샘플 수}

    제한 걸림은 마지막 샘플 하나가 아니라 창 안의 어느 샘플이든 Active 였는지 본다. 제한은
    순간적으로 켜졌다 꺼질 수 있어서 마지막 샘플만 보면 경고를 놓치기 쉽다.
    """
    path = run_dir / GPU_FILE
    try:
        pos = max(0, path.stat().st_size - _GPU_TAIL_BYTES)
    except OSError:
        return None
    # 서버가 0.5초마다 부르므로 파일 전체가 아니라 끝부분만 읽는다. 잘린 첫 줄은 read_new_records 가
    # 깨진 줄로 건너뛰고, 래퍼가 쓰는 중인 마지막 줄은 개행이 없어 소비하지 않는다.
    samples = read_new_records(path, pos)[0][-GPU_WINDOW:]
    if not samples or time.time() - samples[-1].get("ts", 0) > GPU_STALE_SEC:
        return None
    utils = [s["util"] for s in samples if s.get("util") is not None]
    return {
        "latest": samples[-1],
        "util_avg": round(sum(utils) / len(utils), 1) if utils else None,
        "power_cap": any(s.get("sw_power_cap") for s in samples),
        "thermal": any(s.get("sw_thermal") or s.get("hw_thermal") for s in samples),
        "samples": len(samples),
    }
