"""테스트 세션 공통 설정.

여기 있는 유일한 이유는 torch DLL 선점이다. run.py / scripts/to_lerobot.py 상단과
같은 패턴 — Windows 에서 cv2/pandas 가 torch 보다 먼저 로드되면 동명의 런타임
DLL(OpenMP 계열)을 먼저 점유해 torch 초기화가 WinError 1114 로 깨진다.

pytest 는 test_analysis.py(cv2 사용) 같은 모듈을 test_episodes.py(torch 사용)보다
먼저 수집하므로, 진입점에 선점이 있어도 테스트에서는 순서가 다시 뒤집힌다
(실측: tests/test_episodes.py 가 수집 단계에서 c10.dll 로드 실패로 죽었다).
conftest 는 어떤 테스트 모듈보다 먼저 임포트되므로 여기가 선점 자리다.
"""

try:
    import torch  # noqa: F401
except ImportError:
    # torch 미설치 환경(=모방학습을 안 쓰는 경우)에서는 조용히 넘어간다.
    # 그 경우 test_episodes.py 는 pytest 가 알아서 스킵/에러 처리한다.
    pass
