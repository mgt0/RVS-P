"""실제 학습 로그를 대시보드 차트에 따라 그리는 경로 — 파서 · 래퍼 변환 · GPU 계측 · 팔로워.

lerobot 은 설치하지 않는다. 로그 줄은 lerobot>=0.5 의 로거 포맷
"%(levelname)s %(lerobot_pid)s%(asctime)s %(lerobot_location)15s %(message)s" 를 따른 합성이고,
nvidia-smi 도 부르지 않는다(출력 한 줄을 합성하거나 python -c 로 흉내 낸다).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backend.core.act_trainer import (  # noqa: E402
    DemoTrainer,
    LeRobotTrainer,
    TrainingController,
    TrainingManager,
)
from backend.core.train_log import (  # noqa: E402
    GPU_FILE,
    GPU_STALE_SEC,
    HEARTBEAT_FILE,
    HEARTBEAT_STALE_SEC,
    TrainLogReader,
    latest_run_dir,
    parse_gpu_line,
    parse_line,
    parse_number,
    read_gpu,
    read_new_records,
    read_run,
)
import train_act  # noqa: E402


# ------------------------------------------------------------------ 합성 로그 줄
def metric_line(step: str = "200", loss: str = "5.123", tail: str = "") -> str:
    return (
        f"INFO 2026-10-28 12:34:56 ot_train.py:412 step:{step} smpl:1K ep:0 epch:0.05 "
        f"loss:{loss} grdn:99.123 lr:1.0e-05 data_s:0.002 updt_s:0.087 step_s:0.089 smp/s:96 {tail}"
    ).strip()


def eval_line(step: int, loss: str = "0.1234") -> str:
    return f"INFO 2026-10-28 12:35:10 ot_train.py:450 step {step}: eval_loss={loss}"


# nvidia-smi --query-gpu=<GPU_QUERY> --format=csv,noheader,nounits 가 찍는 한 줄 (끝의 셋은 클럭 제한 사유)
SMI_LINE = "97, 6021, 8151, 98.45, 115.00, 71, 2100, Not Active, Active, Not Active"


def gpu_sample(**over) -> dict:
    """gpu.jsonl 한 줄과 같은 모양의 샘플. ts 는 지금."""
    return {"ts": time.time(), "util": 90.0, "mem_used": 6000.0, "mem_total": 8151.0, "power": 95.0,
            "power_limit": 115.0, "temp": 70.0, "sm_clock": 2100.0,
            "sw_power_cap": False, "sw_thermal": False, "hw_thermal": False, **over}


def write_gpu(run_dir: Path, samples) -> None:
    with open(run_dir / GPU_FILE, "a", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")


def cmd_for(*argv: str) -> list[str]:
    """래퍼 인자 -> lerobot-train 명령줄."""
    args, extra = train_act.split_args(["--repo-id", "omx/scan", *argv])
    return train_act.build_command(["lerobot-train"], args, extra, Path("out"))


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------ 파서
class TestParse:
    def test_지표_줄을_key_value_로_읽는다(self):
        rec = parse_line(metric_line())
        assert rec["kind"] == "train"
        assert rec["step_approx"] == 200
        assert rec["loss"] == pytest.approx(5.123)
        assert rec["grdn"] == pytest.approx(99.123)
        assert rec["lr"] == pytest.approx(1e-5)
        assert rec["smp/s"] == 96
        assert rec["smpl"] == 1000   # 'smpl:1K'

    def test_로거_접두어의_위치_필드는_지표로_새지_않는다(self):
        rec = parse_line(metric_line())
        assert "ot_train.py" not in rec
        assert not any(k.startswith(("INFO", "2026")) for k in rec)

    def test_일부_미터가_없거나_조건부_미터가_있어도_읽는다(self):
        assert parse_line("INFO 2026-10-28 12:00:00 x.py:1 step:50 loss:1.500") == {
            "kind": "train", "step_approx": 50, "loss": 1.5}
        rec = parse_line(metric_line(tail="grad_scale:1.0e+03 gpu_mem_gb:3.210"))
        assert rec["grad_scale"] == pytest.approx(1000)
        assert rec["gpu_mem_gb"] == pytest.approx(3.21)

    def test_접미사_숫자를_복원한다(self):
        assert parse_number("25K") == 25_000
        assert parse_number("1.5M") == 1_500_000
        assert parse_number("2B") == 2e9
        assert parse_number("1.0e-05") == pytest.approx(1e-5)
        assert parse_number("-0.5") == -0.5
        assert parse_line(metric_line(step="1K"))["step_approx"] == 1000

    @pytest.mark.parametrize("bad", ["nan", "inf", "abc", "", "1KK", "K"])
    def test_숫자가_아니거나_유한하지_않으면_None(self, bad):
        assert parse_number(bad) is None

    def test_평가_줄(self):
        assert parse_line(eval_line(1000)) == {"kind": "eval", "step": 1000, "eval_loss": 0.1234}
        assert parse_line(eval_line(1000, "nan")) is None

    @pytest.mark.parametrize("line", [
        "",
        "INFO 2026-10-28 12:00:00 ot_train.py:100 Creating dataset",
        "INFO 2026-10-28 12:00:00 ot_train.py:101 cfg.steps=100000 (100K)",
        "INFO 2026-10-28 12:00:00 ot_train.py:102 Output dir: outputs/train/2026-10-28/12-00-00_act",
        "WARNING 2026-10-28 12:00:00 x.py:1 step_s:0.123 only",   # step_s 는 step: 이 아니다
        "INFO 2026-10-28 12:00:00 x.py:1 step: 5",                 # 값이 붙어있지 않다
        "INFO 2026-10-28 12:00:00 x.py:1 step:abc loss:1.0",
    ])
    def test_무관한_줄은_None(self, line):
        assert parse_line(line) is None


# ------------------------------------------------------------------ 스텝 복원
class TestStepRestore:
    def test_손실_압축된_step_도_줄_번호로_복원한다(self):
        # 1000/1200/1400 이 전부 'step:1K' 로 찍힌다 — 파싱값으로는 구분이 안 된다.
        reader = TrainLogReader(log_freq=200)
        steps = [reader.feed(metric_line(step="1K"))["step"] for _ in range(3)]
        assert steps == [200, 400, 600]

    def test_log_freq_를_모르면_근사값(self):
        reader = TrainLogReader()
        assert reader.feed(metric_line(step="1K"))["step"] == 1000
        assert reader.feed(metric_line(step="2K"))["step"] == 2000

    def test_무관한_줄과_평가_줄은_줄_번호를_세지_않는다(self):
        reader = TrainLogReader(log_freq=50)
        assert reader.feed("INFO ... Creating dataset") is None
        assert reader.feed(metric_line())["step"] == 50
        assert reader.feed(eval_line(50))["step"] == 50     # 평가 줄의 step 은 정확한 값 그대로
        assert reader.feed(metric_line())["step"] == 100

    def test_loss_가_nan_인_줄도_스텝은_센다(self):
        reader = TrainLogReader(log_freq=50)
        bad = reader.feed(metric_line(loss="nan"))
        assert bad["step"] == 50 and "loss" not in bad
        assert reader.feed(metric_line())["step"] == 100


# ------------------------------------------------------------------ GPU 계측 파서 · 요약
class TestGpu:
    def test_nvidia_smi_한_줄을_샘플로_바꾼다(self):
        assert parse_gpu_line(SMI_LINE) == {
            "util": 97, "mem_used": 6021, "mem_total": 8151, "power": 98.45, "power_limit": 115,
            "temp": 71, "sm_clock": 2100,
            "sw_power_cap": False, "sw_thermal": True, "hw_thermal": False}   # Active -> True

    def test_지원하지_않는_항목은_None(self):
        rec = parse_gpu_line("[N/A], 6021, 8151, [N/A], 115.00, 71, 2100, [N/A], [Not Supported], Not Active")
        assert (rec["util"], rec["power"], rec["mem_used"]) == (None, None, 6021)
        assert (rec["sw_power_cap"], rec["sw_thermal"], rec["hw_thermal"]) == (None, None, False)

    @pytest.mark.parametrize("bad", ["", " \n", "No devices were found", "97, 6021", SMI_LINE + ", 1"])
    def test_열_수가_안_맞거나_비면_None(self, bad):
        assert parse_gpu_line(bad) is None

    def test_GPU_가_여럿이면_첫_줄만(self):
        assert parse_gpu_line(f"{SMI_LINE}\n1, 2, 3, 4, 5, 6, 7, Active, Active, Active\n")["util"] == 97

    def test_최근_창의_평균과_제한_걸림을_요약한다(self, tmp_path):
        # 맨 앞 샘플(util 10, 전력 제한)은 창(6개) 밖이라 평균에도 경고에도 안 센다
        write_gpu(tmp_path, [gpu_sample(util=10.0, sw_power_cap=True)]
                  + [gpu_sample(util=u) for u in (80.0, 90.0, 100.0, 90.0, 80.0, 100.0)])
        g = read_gpu(tmp_path)
        assert g["samples"] == 6 and g["util_avg"] == 90.0
        assert g["power_cap"] is False and g["thermal"] is False
        assert g["latest"]["util"] == 100.0 and g["latest"]["power_limit"] == 115.0
        json.dumps(g)    # SSE 가 그대로 직렬화한다

    @pytest.mark.parametrize("key, warn, other", [
        ("sw_power_cap", "power_cap", "thermal"),
        ("sw_thermal", "thermal", "power_cap"),
        ("hw_thermal", "thermal", "power_cap"),
    ])
    def test_창_안의_어느_샘플이든_Active_면_경고(self, tmp_path, key, warn, other):
        # 제한은 순간적으로 켜졌다 꺼진다 — 마지막 샘플이 Not Active 여도 경고는 남는다
        write_gpu(tmp_path, [gpu_sample(), gpu_sample(**{key: True}), gpu_sample()])
        g = read_gpu(tmp_path)
        assert g[warn] is True and g[other] is False

    def test_샘플이_없거나_낡았으면_None(self, tmp_path):
        assert read_gpu(tmp_path) is None                                   # 파일 없음
        write_gpu(tmp_path, [gpu_sample(ts=time.time() - GPU_STALE_SEC - 5)])
        assert read_gpu(tmp_path) is None                                   # 계측이 멈춘 지 오래

    def test_util_이_None_인_샘플은_평균에서_뺀다(self, tmp_path):
        write_gpu(tmp_path, [gpu_sample(util=None), gpu_sample(util=80.0)])
        assert read_gpu(tmp_path)["util_avg"] == 80.0
        only_none = tmp_path / "only_none"
        only_none.mkdir()
        write_gpu(only_none, [gpu_sample(util=None)])
        assert read_gpu(only_none)["util_avg"] is None

    def test_큰_파일은_끝부분만_읽고_쓰는_중인_줄은_무시한다(self, tmp_path):
        # 파일이 읽기 크기(4KB)보다 훨씬 커도 같은 요약이 나온다. 잘린 첫 줄과 쓰는 중인 끝 줄은 건너뛴다.
        write_gpu(tmp_path, [gpu_sample(util=50.0)] * 100 + [gpu_sample(util=100.0)] * 6)
        with open(tmp_path / GPU_FILE, "a", encoding="utf-8") as f:
            f.write('{"ts": 1, "ut')
        g = read_gpu(tmp_path)
        assert g["samples"] == 6 and g["util_avg"] == 100.0


# ------------------------------------------------------------------ 래퍼(lerobot 없이)
class TestWrapper:
    def test_pump_는_원본을_보존하고_지표만_뽑는다(self):
        raw = (
            "INFO ... Creating dataset 한글 로그\n"
            f"{metric_line()}\n"
            f"{eval_line(50)}\n"
            f"{metric_line(step='1K', loss='4.000')}\n"
        ).encode("utf-8") + b"\xff bad byte\n" + metric_line().encode()   # 마지막 줄은 개행 없음
        log_fp, metrics_fp, echoed = io.BytesIO(), io.StringIO(), []

        train_act.pump(io.BytesIO(raw), log_fp, metrics_fp, TrainLogReader(50), echoed.append)

        assert log_fp.getvalue() == raw                      # train.log 는 원본 그대로
        assert len(echoed) == 6                              # 콘솔에도 줄마다 흘렸다
        recs = [json.loads(line) for line in metrics_fp.getvalue().splitlines()]
        assert [(r["kind"], r["step"]) for r in recs] == [
            ("train", 50), ("eval", 50), ("train", 100), ("train", 150)]
        assert recs[2]["loss"] == pytest.approx(4.0)

    def test_명령줄은_README_학습_명령과_맞는다(self):
        args, extra = train_act.split_args(["--repo-id", "omx/scan", "--steps", "20000"])
        cmd = train_act.build_command(["lerobot-train"], args, extra, Path("out"))
        assert cmd[0] == "lerobot-train"
        for expected in ("--dataset.repo_id=omx/scan", "--policy.type=act", "--steps=20000",
                         "--policy.repo_id=omx/scan_act", "--policy.push_to_hub=false",
                         "--log_freq=50", f"--output_dir={Path('out')}"):
            assert expected in cmd
        assert not any(a.startswith("--batch_size") for a in cmd)   # 안 준 인자는 lerobot 기본값

    def test_dash_dash_뒤_인자는_그대로_넘기고_기본값은_양보한다(self):
        args, extra = train_act.split_args(
            ["--repo-id", "omx/scan", "--", "--policy.device=cuda", "--policy.repo_id=me/act"])
        cmd = train_act.build_command(["lerobot-train"], args, extra, Path("out"))
        assert cmd[-2:] == ["--policy.device=cuda", "--policy.repo_id=me/act"]
        assert cmd.count("--policy.repo_id=me/act") == 1
        assert not any(a == "--policy.repo_id=omx/scan_act" for a in cmd)

    def test_혼합정밀도는_기본_bf16_이고_인자로_바꾼다(self):
        assert "--accelerator.mixed_precision=bf16" in cmd_for()
        assert "--accelerator.mixed_precision=no" in cmd_for("--precision", "no")
        assert "--accelerator.mixed_precision=fp16" in cmd_for("--precision", "fp16")
        with pytest.raises(SystemExit):
            cmd_for("--precision", "fp8")     # 모르는 값은 lerobot 에 넘기기 전에 거부한다

    def test_num_workers_는_준_때만_넘기고_0도_넘긴다(self):
        assert not any(a.startswith("--num_workers") for a in cmd_for())   # 안 주면 lerobot 기본값
        assert "--num_workers=8" in cmd_for("--num-workers", "8")
        assert "--num_workers=0" in cmd_for("--num-workers", "0")          # 0 = 메인 프로세스 로딩, 유효한 값

    @pytest.mark.parametrize("extra", [
        ["--log_freq=10"], ["--steps", "5"], ["--output_dir=x"],
        ["--accelerator.mixed_precision=no"], ["--num_workers", "2"],
    ])
    def test_래퍼가_관리하는_인자가_겹치면_거부한다(self, extra):
        # --log_freq 가 몰래 바뀌면 스텝 복원(x축)이 조용히 틀어진다.
        with pytest.raises(SystemExit):
            train_act.split_args(["--repo-id", "omx/scan", "--", *extra])

    def test_repo_id_는_필수(self):
        with pytest.raises(SystemExit):
            train_act.split_args(["--steps", "10"])

    def test_lerobot_탐색(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
        monkeypatch.setattr(train_act.importlib.util, "find_spec", lambda name: None)
        assert train_act.find_lerobot_train() is None                 # 미설치면 None -> 설치 안내

        (tmp_path / "Scripts").mkdir()
        (tmp_path / "Scripts" / "lerobot-train.exe").write_bytes(b"")
        assert train_act.find_lerobot_train() == [str(tmp_path / "Scripts" / "lerobot-train.exe")]

        (tmp_path / "Scripts" / "lerobot-train.exe").unlink()
        monkeypatch.setattr(train_act.importlib.util, "find_spec", lambda name: object())
        assert train_act.find_lerobot_train() == [
            str(tmp_path / "python.exe"), "-m", "lerobot.scripts.lerobot_train"]

    def test_accelerator_지원은_패키지_폴더의_configs_accelerator_py_로_가린다(self, tmp_path, monkeypatch):
        pkg = tmp_path / "lerobot"
        (pkg / "configs").mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))        # 임포트하지 않고 위치만 찾는다
        assert train_act.lerobot_has_accelerator() is False        # 릴리스(0.6.1 까지) 모양
        (pkg / "configs" / "accelerator.py").write_text("", encoding="utf-8")
        assert train_act.lerobot_has_accelerator() is True         # main 모양
        monkeypatch.setattr(train_act.importlib.util, "find_spec", lambda name: None)
        assert train_act.lerobot_has_accelerator() is False        # 미설치


# ------------------------------------------------------------------ GPU 계측 스레드
OK_SMI = subprocess.CompletedProcess([], 0, SMI_LINE + "\n", "")
BAD_SMI = [    # 실행은 되는데 실패하는 세 가지
    subprocess.TimeoutExpired("nvidia-smi", 10),
    subprocess.CompletedProcess([], 9, "", "boom"),                          # 0 이 아닌 종료 코드
    subprocess.CompletedProcess([], 0, "No devices were found", ""),         # 출력을 못 읽음
]


def fake_smi(monkeypatch, outcomes, stop=None) -> list:
    """subprocess.run 을 outcomes 를 차례로 내놓는 가짜로 바꾼다(예외는 던진다). 호출 기록을 돌려준다.

    stop 을 주면 마지막 호출에서 켠다 — sample_gpu 가 그 결과까지 처리하고 끝난다.
    """
    calls: list = []

    def run(*args, **kwargs):
        i = len(calls)
        calls.append(args)
        assert i < len(outcomes), "sample_gpu 가 멈추지 않고 계속 부른다"
        if stop is not None and i == len(outcomes) - 1:
            stop.set()
        out = outcomes[i]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(train_act.subprocess, "run", run)
    return calls


class TestGpuSampler:
    def test_nvidia_smi_가_없으면_한_번_알리고_바로_끝난다(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(train_act, "_SMI_CMD", ["nvidia-smi-없는-명령"])
        train_act.sample_gpu(tmp_path / GPU_FILE, threading.Event(), 0.01)    # 다시 시도하지 않고 바로 돌아온다
        assert not (tmp_path / GPU_FILE).exists()
        assert capsys.readouterr().err.count("GPU 계측") == 1

    def test_계속_실패하면_연속_한도에서_멈추고_안내는_첫_실패와_멈출_때만(self, tmp_path, monkeypatch, capsys):
        limit = train_act._GPU_MAX_FAILS
        calls = fake_smi(monkeypatch, [BAD_SMI[i % 3] for i in range(limit + 5)])   # 세 가지 실패를 돌아가며
        train_act.sample_gpu(tmp_path / GPU_FILE, threading.Event(), 0)
        assert len(calls) == limit
        assert not (tmp_path / GPU_FILE).exists()
        assert capsys.readouterr().err.count("GPU 계측") == 2      # 첫 실패, 그리고 멈춤

    def test_일시적_실패는_다음_주기에_다시_시도하고_성공하면_연속_횟수를_지운다(self, tmp_path, monkeypatch, capsys):
        stop, path = threading.Event(), tmp_path / GPU_FILE
        almost = [BAD_SMI[i % 3] for i in range(train_act._GPU_MAX_FAILS - 1)]   # 한도 직전까지 실패했다가
        calls = fake_smi(monkeypatch, [*almost, OK_SMI, *almost, OK_SMI], stop)  # 성공하면 처음부터 센다
        train_act.sample_gpu(path, stop, 0)
        assert len(calls) == 2 * len(almost) + 2                   # 멈추지 않고 끝까지 돌았다
        assert len(path.read_text(encoding="utf-8").splitlines()) == 2
        assert capsys.readouterr().err.count("GPU 계측") == 1      # 안내는 첫 실패에서만

    def test_nvidia_smi_출력을_gpu_jsonl_에_쌓고_읽는_쪽과_스키마가_맞는다(self, tmp_path, monkeypatch):
        monkeypatch.setattr(train_act, "_SMI_CMD", [sys.executable, "-c", f"print({SMI_LINE!r})"])
        path, stop = tmp_path / GPU_FILE, threading.Event()
        t = threading.Thread(target=train_act.sample_gpu, args=(path, stop, 0.01), daemon=True)
        t.start()
        assert wait_until(lambda: path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= 2,
                          timeout=30)
        stop.set()
        t.join(10)
        assert not t.is_alive()
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert first["util"] == 97 and first["sw_thermal"] is True and first["ts"] > 0
        assert read_gpu(tmp_path)["util_avg"] == 97     # 쓰는 쪽(래퍼)과 읽는 쪽(서버)의 스키마가 같다


# ------------------------------------------------------------------ 래퍼 main() — 가짜 lerobot-train
# sleep: GPU 계측 스레드가 (실패하는) 첫 호출과 안내를 끝낸 뒤에 학습이 끝나게 한다. 종료 신호가 먼저
# 가면 안내를 생략하는 것이 정상 동작이라, 그 순서가 뒤집히면 안내 횟수를 세는 테스트가 흔들린다.
FAKE_TRAIN = ("import time; time.sleep(0.3); "
              "print('INFO 2026-10-28 12:34:56 ot_train.py:412 step:50 loss:1.500 lr:1.0e-05')")


@pytest.fixture
def fake_main(tmp_path, monkeypatch):
    """main() 을 lerobot·GPU 없이 돌리는 환경: 가짜 lerobot-train, 임시 로그 루트, nvidia-smi 없음."""
    monkeypatch.setattr(train_act, "TRAIN_LOG_ROOT", tmp_path)
    monkeypatch.setattr(train_act, "find_lerobot_train", lambda: [sys.executable, "-c", FAKE_TRAIN])
    monkeypatch.setattr(train_act, "_SMI_CMD", ["nvidia-smi-없는-명령"])
    monkeypatch.setattr(train_act, "lerobot_has_accelerator", lambda: False)

    def run(*argv: str) -> Path:
        assert train_act.main(["--repo-id", "omx/scan", *argv]) == 0
        (run_dir,) = tmp_path.iterdir()
        return run_dir
    return run


class TestMain:
    def test_GPU_계측이_안_돼도_학습은_끝나고_기록이_남는다(self, fake_main, capsys):
        run_dir = fake_main("--num-workers", "2")
        meta = read_run(run_dir)
        assert meta["status"] == "DONE" and meta["returncode"] == 0
        assert meta["num_workers"] == 2 and "--num_workers=2" in meta["cmd"]
        recs = [json.loads(ln) for ln in (run_dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [(r["kind"], r["step"]) for r in recs] == [("train", 50)]    # 학습 기록은 정상
        assert not (run_dir / GPU_FILE).exists()                            # 계측 기록은 없고
        assert capsys.readouterr().err.count("GPU 계측") == 1                # 안내는 한 번
        assert (run_dir / HEARTBEAT_FILE).is_file()                         # 생존 신호는 GPU 계측과 따로 남는다

    def test_같은_초에_뜬_래퍼는_접미사로_구분하고_최신_순서가_유지된다(self, fake_main, monkeypatch, tmp_path):
        class SameSecond(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 28, 10, 0, 0)

        monkeypatch.setattr(train_act, "datetime", SameSecond)
        stamp = "20261028_100000"
        for name in [stamp, *(f"{stamp}_{n:02d}" for n in range(2, 10))]:   # 같은 초에 이미 9개가 떴다
            make_run(tmp_path, name, "DONE")

        assert train_act.main(["--repo-id", "omx/scan"]) == 0              # 10번째는 _10
        assert latest_run_dir(tmp_path).name == f"{stamp}_10"             # 한 자리 접미사였다면 _9 가 더 최신으로 정렬된다
        meta = read_run(tmp_path / f"{stamp}_10")
        assert meta["status"] == "DONE" and meta["output_dir"].endswith(f"{stamp}_10")   # 체크포인트 폴더도 겹치지 않는다

    @pytest.mark.parametrize("supported, expected", [(True, "bf16"), (False, None)])
    def test_lerobot_가_accelerator_를_모르면_혼합정밀도_인자를_생략한다(
            self, fake_main, monkeypatch, capsys, supported, expected):
        monkeypatch.setattr(train_act, "lerobot_has_accelerator", lambda: supported)
        meta = read_run(fake_main())
        assert meta["precision"] == expected     # run.json 에는 실제로 쓴 값이 남는다
        assert ("--accelerator.mixed_precision=bf16" in meta["cmd"]) is supported
        assert ("--accelerator.mixed_precision" in capsys.readouterr().err) is (not supported)   # 생략할 때만 안내


# ------------------------------------------------------------------ 증분 읽기
def test_쓰는_중인_마지막_줄은_다음_번에_읽는다(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_bytes(b'{"kind": "train", "step": 50, "loss": 1.0}\n{"kind": "tra')
    recs, pos = read_new_records(path, 0)
    assert [r["step"] for r in recs] == [50]

    with open(path, "ab") as f:
        f.write(b'in", "step": 100, "loss": 0.9}\nnot json\n[1, 2]\n')
    recs, pos = read_new_records(path, pos)
    assert [r["step"] for r in recs] == [100]     # 깨진 줄·dict 아닌 줄은 건너뛴다
    assert read_new_records(path, pos) == ([], pos)
    assert read_new_records(tmp_path / "없음.jsonl", 0) == ([], 0)


# ------------------------------------------------------------------ 팔로워
def make_run(root: Path, name: str, status: str, log_lines=(), *, returncode=None) -> Path:
    """래퍼가 남기는 것과 같은 모양의 실행 폴더. metrics.jsonl 은 실제 변환(pump)으로 만든다."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    metrics = io.StringIO()
    train_act.pump([f"{ln}\n".encode() for ln in log_lines], io.BytesIO(), metrics, TrainLogReader(50))
    (run_dir / "metrics.jsonl").write_text(metrics.getvalue(), encoding="utf-8")
    set_status(run_dir, status, returncode)
    return run_dir


def set_status(run_dir: Path, status: str, returncode=None) -> None:
    (run_dir / "run.json").write_text(json.dumps(
        {"status": status, "returncode": returncode, "log_freq": 50, "steps": 20000}), encoding="utf-8")


def age_heartbeat(run_dir: Path, age_sec: float) -> None:
    """heartbeat 파일을 age_sec 초 전에 마지막으로 갱신된 것으로 만든다."""
    path = run_dir / HEARTBEAT_FILE
    path.touch()
    past = time.time() - age_sec
    os.utime(path, (past, past))


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(LeRobotTrainer, "POLL_SEC", 0.02)


class TestHeartbeat:
    def test_RUNNING_인데_heartbeat_가_낡았으면_FAILED_와_stale(self, tmp_path):
        set_status(tmp_path, "RUNNING")
        age_heartbeat(tmp_path, HEARTBEAT_STALE_SEC + 5)
        meta = read_run(tmp_path)
        assert meta["status"] == "FAILED" and meta["stale"] is True
        assert meta["steps"] == 20000                                  # 나머지 필드는 그대로

    def test_heartbeat_가_새것이면_RUNNING_그대로(self, tmp_path):
        set_status(tmp_path, "RUNNING")
        age_heartbeat(tmp_path, HEARTBEAT_STALE_SEC / 2)
        meta = read_run(tmp_path)
        assert meta["status"] == "RUNNING" and "stale" not in meta

    def test_heartbeat_가_없는_옛_실행은_RUNNING_그대로(self, tmp_path):
        set_status(tmp_path, "RUNNING")
        assert read_run(tmp_path)["status"] == "RUNNING"

    @pytest.mark.parametrize("status", ["DONE", "FAILED"])
    def test_끝난_실행은_heartbeat_가_낡아도_건드리지_않는다(self, tmp_path, status):
        set_status(tmp_path, status, 0)
        age_heartbeat(tmp_path, HEARTBEAT_STALE_SEC * 10)
        assert "stale" not in read_run(tmp_path)

    def test_래퍼가_죽은_실행을_따라_읽다가_영원히_기다리지_않고_FAILED(self, tmp_path):
        run_dir = make_run(tmp_path, "20261028_100000", "RUNNING", [metric_line()])
        age_heartbeat(run_dir, HEARTBEAT_STALE_SEC + 5)
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)                             # 폴링 루프가 바로 끝난다
        assert ctrl.state == "FAILED" and ctrl.status()["num_epochs"] == 1


class TestFollower:
    def test_끝난_실행은_전체_곡선을_채우고_DONE(self, tmp_path):
        make_run(tmp_path, "20261028_100000", "DONE", [
            metric_line(loss="5.000"), metric_line(loss="4.000"), eval_line(100, "0.3000"),
            metric_line(loss="3.000"),
        ])
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)

        snap = ctrl.status()
        assert snap["state"] == "DONE" and snap["error"] is None
        recs = snap["records"]
        assert [r["epoch"] for r in recs] == [50, 100, 150]          # epoch 자리에 복원 step
        assert [r["train_loss"] for r in recs] == [5.0, 4.0, 3.0]
        assert [r["val_loss"] for r in recs] == [None, 0.3, None]    # 평가 줄이 있던 스텝에만
        assert all(r["val_mae_deg"] is None and r["val_success_rate"] is None for r in recs)
        assert recs[0]["lr"] == pytest.approx(1e-5)
        assert (snap["best_epoch"], snap["best_val_loss"]) == (100, 0.3)
        assert snap["params"]["total_steps"] == 20000 and snap["params"]["log_freq"] == 50
        json.dumps(snap)    # SSE 가 그대로 직렬화한다

    def test_평가_스텝이_지표_줄과_안_맞으면_직전_train_loss_로_별도_점(self, tmp_path):
        make_run(tmp_path, "20261028_100000", "DONE", [
            metric_line(loss="5.000"), eval_line(75, "0.2000"), metric_line(loss="4.000")])
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)
        recs = ctrl.status()["records"]
        assert [(r["epoch"], r["train_loss"], r["val_loss"]) for r in recs] == [
            (50, 5.0, None), (75, 5.0, 0.2), (100, 4.0, None)]

    def test_진행중인_실행을_따라가다_DONE_에서_마지막_줄까지_읽고_끝난다(self, tmp_path, fast_poll):
        run_dir = make_run(tmp_path, "20261028_100000", "RUNNING", [metric_line(loss="5.000")])
        ctrl = TrainingController({})
        t = threading.Thread(target=LeRobotTrainer(tmp_path).run, args=(ctrl,), daemon=True)
        t.start()
        assert wait_until(lambda: ctrl.status()["num_epochs"] == 1)
        assert ctrl.state == "RUNNING"

        # 래퍼처럼: 지표를 먼저 쓰고, 그 '뒤에' 상태를 DONE 으로 바꾼다.
        with open(run_dir / "metrics.jsonl", "a", encoding="utf-8") as f:
            for step, loss in ((100, 4.0), (150, 3.0)):
                f.write(json.dumps({"kind": "train", "step": step, "loss": loss}) + "\n")
        assert wait_until(lambda: ctrl.status()["num_epochs"] == 3)
        with open(run_dir / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "train", "step": 200, "loss": 2.0}) + "\n")
        set_status(run_dir, "DONE", 0)

        t.join(5)
        assert not t.is_alive() and ctrl.state == "DONE"
        assert [r["epoch"] for r in ctrl.status()["records"]] == [50, 100, 150, 200]

    def test_진행중인_실행의_GPU_요약을_상태에_싣고_끝나면_거둔다(self, tmp_path, fast_poll):
        run_dir = make_run(tmp_path, "20261028_100000", "RUNNING", [metric_line()])
        write_gpu(run_dir, [gpu_sample(util=80.0), gpu_sample(util=100.0, sw_power_cap=True)])
        ctrl = TrainingController({})
        assert ctrl.status()["gpu"] is None                   # 시작 전에는 없다
        t = threading.Thread(target=LeRobotTrainer(tmp_path).run, args=(ctrl,), daemon=True)
        t.start()
        assert wait_until(lambda: ctrl.status()["gpu"] is not None)
        snap = ctrl.status()
        gpu = snap["gpu"]
        assert gpu["util_avg"] == 90.0 and gpu["power_cap"] is True and gpu["thermal"] is False
        assert gpu["latest"]["power_limit"] == 115.0
        json.dumps(snap)                                      # SSE 가 그대로 직렬화한다

        set_status(run_dir, "DONE", 0)
        t.join(5)
        assert ctrl.state == "DONE" and ctrl.status()["gpu"] is None   # 끝난 학습에 낡은 GPU 값을 남기지 않는다

    def test_실패한_학습은_FAILED_와_원인_안내(self, tmp_path):
        make_run(tmp_path, "20261028_100000", "FAILED", [metric_line()], returncode=3)
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)
        snap = ctrl.status()
        assert snap["state"] == "FAILED" and snap["num_epochs"] == 1   # 실패 전 기록은 남는다
        assert "returncode=3" in snap["error"] and "train.log" in snap["error"]

    def test_STOP_은_따라_읽기만_끊고_학습_기록은_건드리지_않는다(self, tmp_path, fast_poll):
        run_dir = make_run(tmp_path, "20261028_100000", "RUNNING", [metric_line()])
        before = (run_dir / "run.json").read_text(encoding="utf-8")
        ctrl = TrainingController({})
        t = threading.Thread(target=LeRobotTrainer(tmp_path).run, args=(ctrl,), daemon=True)
        t.start()
        assert wait_until(lambda: ctrl.status()["num_epochs"] == 1)

        ctrl.request_stop()
        t.join(5)
        assert not t.is_alive()
        assert ctrl.state == "STOPPED" and "계속 실행" in ctrl.error
        assert (run_dir / "run.json").read_text(encoding="utf-8") == before   # 여전히 RUNNING

    def test_실행_기록이_없으면_래퍼_사용법을_안내(self, tmp_path):
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path / "없는_폴더").run(ctrl)
        assert ctrl.state == "FAILED"
        assert "scripts/train_act.py" in ctrl.error and "scripts/to_lerobot.py" in ctrl.error

    def test_가장_최근_실행을_따른다(self, tmp_path):
        make_run(tmp_path, "20261027_090000", "DONE", [metric_line()])
        make_run(tmp_path, "20261028_100000", "DONE", [metric_line(), metric_line()])
        (tmp_path / "20261029_110000").mkdir()    # run.json 이 없는 폴더는 건너뛴다
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)
        assert ctrl.status()["num_epochs"] == 2
        assert ctrl.params["run_id"] == "20261028_100000"

    def test_매니저와_API_경로(self, tmp_path):
        make_run(tmp_path / "logs", "20261028_100000", "DONE", [metric_line(), metric_line()])
        mgr = TrainingManager(tmp_path / "data", log_root=tmp_path / "logs")
        mgr.start({}, demo=False)
        assert wait_until(lambda: mgr.status()["state"] == "DONE")
        snap = mgr.status()
        assert snap["num_epochs"] == 2
        assert snap["params"]["mode"] == "lerobot"      # 프론트가 STEP 라벨을 정하는 근거


def test_DEMO_학습에는_gpu_가_없다():
    ctrl = TrainingController({"epochs": 1, "epoch_delay_sec": 0})
    DemoTrainer().run(ctrl)
    assert ctrl.state == "DONE" and ctrl.status()["gpu"] is None


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from backend.app import create_app
    from backend.config import load_config

    root = tmp_path_factory.mktemp("data")
    cfg = load_config()
    cfg = replace(cfg, recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    make_run(root / "logs", "20261028_100000", "DONE", [metric_line(), eval_line(50), metric_line()])
    hub.training._log_root = root / "logs"    # 실제 logs/train 을 읽지 않게 한다
    with app.test_client() as c:
        yield c
    hub.shutdown()


def follow_via_api(client) -> dict:
    assert client.post("/api/train/start", json={"demo": False}).status_code == 200
    assert wait_until(lambda: client.get("/api/train/status").get_json()["training"]["state"] == "DONE")
    return client.get("/api/train/status").get_json()["training"]


class TestApi:
    def test_train_start_demo_false_가_로그를_따라_그린다(self, client):
        t = follow_via_api(client)
        assert [x["epoch"] for x in t["records"]] == [50, 100]
        assert t["records"][0]["val_loss"] == 0.1234      # 같은 스텝의 평가 줄이 붙었다
        assert t["params"]["mode"] == "lerobot"
        assert t["gpu"] is None                           # 끝난 실행 — 키는 있고 값은 없다

    def test_SSE_스트림도_같은_스냅샷을_내보낸다(self, client):
        follow_via_api(client)
        body = client.get("/api/train/stream").get_data(as_text=True)
        frames = [json.loads(ln[len("data: "):]) for ln in body.splitlines() if ln.startswith("data: ")]
        assert frames and frames[-1]["state"] == "DONE"
        assert frames[-1]["num_epochs"] == 2
