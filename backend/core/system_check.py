"""기동 환경 점검 — 진입점이 남긴 torch 선점 import 결과와 패키지 설치 사실.

이 모듈은 import 가 가볍다. cv2·pandas·numpy 를 import 하지 않는다. run.py 는 torch 를
cv2/pandas 보다 먼저 import 해서 DLL 슬롯을 선점해야 하는데(run.py 상단 주석), 이 모듈이
그 선점 자리에 무거운 것을 끌어오면 순서가 깨진다.

torch 자체도 여기서 import 하지 않는다. 진입점이 넘겨준 결과만 보관한다 — 요청 시점에
import 를 시도하면 DLL 이 막힌 PC 에서는 매번 실패하고, 멀쩡한 PC 에서는 서버 프로세스에
torch 를 뒤늦게 올려 선점 순서를 깬다.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib import metadata

_UNRECORDED = object()
_torch_error: object = _UNRECORDED   # None = import 성공, 예외 = 실패, _UNRECORDED = 기록 없음

# 응용 프로그램 제어 정책(스마트 앱 컨트롤·WDAC)이 DLL 로드를 막을 때 Windows 가 돌려주는 코드.
_POLICY_BLOCK = 4551


def record_torch_import(exc: BaseException | None) -> None:
    """진입점이 선점 import 의 결과를 넘긴다. 성공이면 None."""
    global _torch_error
    _torch_error = exc


def is_installed(name: str) -> bool:
    """import 하지 않고 설치 여부만 본다."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def torch_status() -> dict:
    """torch 를 쓸 수 있는가. 이 모듈이 아는 사실만 담는다.

    state: ok | blocked(OSError: DLL 로드 거부) | missing(ImportError) | unknown(기록 없음 + 설치돼 있음)
    policy_block: WinError 4551(응용 프로그램 제어 정책 차단)인가.
    """
    err = _torch_error
    if err is None:
        return {"state": "ok", "error": None, "policy_block": False, "not_installed": False}
    if not isinstance(err, BaseException):
        # 진입점이 기록하지 않았다(gunicorn 등). 설치 여부는 import 없이도 알 수 있지만 로드 성공은 모른다.
        state = "unknown" if is_installed("torch") else "missing"
        return {"state": state, "error": None, "policy_block": False, "not_installed": state == "missing"}
    text = f"{type(err).__name__}: {err}"
    if isinstance(err, OSError):
        policy = getattr(err, "winerror", None) == _POLICY_BLOCK or str(_POLICY_BLOCK) in str(err)
        return {"state": "blocked", "error": text, "policy_block": policy, "not_installed": False}
    # ModuleNotFoundError 라도 torch 가 아니라 torch 가 찾던 다른 모듈이면 '설치 안 됨'이 아니다.
    return {"state": "missing", "error": text, "policy_block": False,
            "not_installed": isinstance(err, ModuleNotFoundError) and err.name == "torch"}


def _row(id_: str, severity: str, title: str, detail: str = "", hint: str = "") -> dict:
    return {"id": id_, "severity": severity, "title": title, "detail": detail, "hint": hint}


def _python_row(major: int, minor: int, micro: int, in_venv: bool) -> dict:
    title = f"Python {major}.{minor}.{micro} " + ("가상환경(.venv)" if in_venv else "(가상환경 아님)")
    if (major, minor) >= (3, 13):
        # CLAUDE.md: numpy wheel 호환 문제로 3.13 에서 깨진다.
        return _row("python", "warn", title, "3.13 이상은 numpy wheel 호환 문제로 설치가 깨집니다.",
                    "3.11 또는 3.12 가상환경을 쓰세요.")
    return _row("python", "ok", title)


def _torch_row() -> dict:
    st = torch_status()
    if st["state"] == "ok":
        version = _dist_version("torch")
        return _row("torch", "ok", f"torch {version} 사용 가능" if version else "torch 사용 가능")
    if st["state"] == "blocked":
        if st["policy_block"]:
            return _row("torch", "warn", "torch 로드 실패",
                        "애플리케이션 제어 정책(Windows 스마트 앱 컨트롤 등)이 torch DLL 을 차단했습니다(WinError 4551). "
                        "CNN 분류·rollout 을 쓸 수 없습니다.",
                        "확인 위치: Windows 보안 › 앱 및 브라우저 컨트롤 › 스마트 앱 컨트롤")
        return _row("torch", "warn", "torch 로드 실패", f"{st['error']} — CNN 분류·rollout 을 쓸 수 없습니다.")
    if st["state"] == "missing":
        title = "torch 미설치" if st["not_installed"] else "torch 로드 실패(ImportError)"
        return _row("torch", "info", title, "CNN 분류·rollout 을 쓸 수 없습니다.", "requirements.txt 의 torch 를 설치하세요.")
    return _row("torch", "info", "torch 상태 확인 불가",
                "서버 진입점이 기동 때 import 결과를 기록하지 않았습니다(gunicorn 등). 패키지는 설치돼 있습니다.")


def _lerobot_row() -> dict:
    version = _dist_version("lerobot")
    if version:
        return _row("lerobot", "ok", f"lerobot {version} 설치됨")
    if is_installed("lerobot"):
        return _row("lerobot", "ok", "lerobot 설치됨")
    return _row("lerobot", "info", "lerobot 미설치", "학습 래퍼·rollout에 필요(requirements-lerobot.txt)")


def _opencv_row(gui: str | None, headless: str | None) -> dict:
    """gui=opencv-python 버전, headless=opencv-python-headless 버전(없으면 None)."""
    if gui and headless:
        return _row("opencv", "warn", "opencv-python 과 opencv-python-headless 가 함께 설치됨",
                    "두 패키지가 섞이면 cv2 파일이 뒤엉켜 DLL 충돌이 납니다(2026-08-11 실측).",
                    "둘 다 지우고 opencv-python-headless 만 다시 설치하세요.")
    if headless:
        return _row("opencv", "ok", "opencv-python-headless 단독 설치", "GUI판과 섞으면 DLL 충돌")
    if gui:
        return _row("opencv", "info", "opencv-python(GUI판) 설치됨",
                    "requirements.txt 는 opencv-python-headless 를 씁니다. 둘을 함께 설치하면 DLL 충돌이 납니다.")
    return _row("opencv", "info", "OpenCV 패키지 정보를 찾지 못함", "cv2 가 pip 가 아닌 경로로 설치됐을 수 있습니다.")


def env_checks() -> list[dict]:
    """진단 화면 '실행 환경' 행. severity: ok | info | warn."""
    v = sys.version_info
    return [
        _python_row(v.major, v.minor, v.micro, sys.prefix != sys.base_prefix),
        _torch_row(),
        _lerobot_row(),
        _opencv_row(_dist_version("opencv-python"), _dist_version("opencv-python-headless")),
    ]
