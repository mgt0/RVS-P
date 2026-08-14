// STM32F411 "BlackPill" (DFRobot DFR0864) + TB6600 ×3 + NEMA17 ×3: 스텝모터 3축 UART 제어
//
//   M1 = 컨베이어 벨트 1 (공급)
//   M2 = 컨베이어 벨트 2 (로봇 작업 영역)
//   M3 = LM 가이드
//
// 빌드 환경 (Arduino IDE)
//   보드 매니저: "STM32 MCU based boards" (STM32duino 공식 코어)
//   Board: Generic STM32F4 series → BlackPill F411CE
//   USB support: "CDC (generic 'Serial' supersede U(S)ART)"  ← 필수. USB 가 Serial 이 된다.
//   업로드: BOOT0 누른 채 리셋 → DFU 모드 → Upload method: STM32CubeProgrammer(DFU)
//
// 명령줄 업로드 (2026-08-14 실제로 통한 절차. IDE 없이 굽고 싶을 때)
//   1) 서버가 COM 포트를 잡고 있으면 먼저 놓아준다: POST /api/conveyor/feed/release
//   2) 컴파일 (arduino-cli 는 Arduino IDE 안에 들어 있다):
//      "%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" \
//        compile -b STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F411CE,usb=CDCgen \
//        --export-binaries firmware/conveyor_blackpill
//   3) BOOT0 누른 채 NRST 눌렀다 떼고 BOOT0 떼기 → "STM32 BOOTLOADER" 로 잡힌다.
//      (소프트웨어 DFU 진입인 1200bps 터치는 이 펌웨어가 지원하지 않는다 — 물리 버튼 필수)
//   4) 굽기. STM32CubeIDE 에 딸린 CubeProgrammer 를 쓴다:
//      C:\ST\STM32CubeIDE_*\...\tools\bin\STM32_Programmer_CLI.exe \
//        -c port=usb1 -w <bin> 0x08000000 -v -g 0x08000000
//   함정 둘. Arduino 에 딸린 dfu-util 은 0.1 구버전이라 STM32 내장 DFU 를 못 굽는다.
//   그리고 CubeProgrammer 는 한글 경로를 못 읽으므로("바탕 화면") .bin 을 ASCII
//   경로로 복사한 뒤 그 경로를 넘겨야 한다.
//
// 배선 (실배선 실측 2026-08-14 — 추천 핀표 1~9번을 순서대로 배정한 것)
//   M1 (컨베이어 1): PUL+ → PA0 (TIM2_CH1), DIR+ → PA1, ENA+ → PA2
//   M2 (컨베이어 2): PUL+ → PA3 (TIM5_CH4), DIR+ → PA6, ENA+ → PA7
//   M3 (LM 가이드) : PUL+ → PB0 (TIM3_CH3), DIR+ → PB1, ENA+ → PA8
//   분류 게이트 서보 신호선 → PB3 (서보 전원은 외부 5V — 스톨 전류가 보드 한계를 넘는다)
//
//   PUL 핀은 모터마다 '서로 다른 타이머'여야 한다(타이머당 PWM 주파수는 1개).
//   함정: PA0 과 PA3 은 핀맵 1순위가 둘 다 TIM2 다. 그래서 PA3 은 대체 기능
//   PA_3_ALT1(TIM5_CH4)로 지정한다 → TIM2/TIM5/TIM3 으로 분리.
//   setup() 이 모터끼리·서보 타이머와의 겹침을 검사해 겹치면 ERR 배너를 뱉는다.
//   ⚠ PA0 는 온보드 KEY 버튼과 연결돼 있다(누르면 GND 직결). 구동 중 누르지 말 것.
//   ⚠ BlackPill 은 3.3V 로직. TB6600 옵토 입력은 5V 기준이라 마진이 빠듯하다
//     (3.3V 로 실증 구동됨). 안 되면 공통 애노드 배선으로 바꾸고 *_ACTIVE_LOW = true.
//
// PC(Flask)가 USB(CDC)로 한 줄 명령을 보내면 한 줄로 응답한다.
// 모든 명령 앞에 "M1 "/"M2 "/"M3 " 를 붙여 모터를 지정한다. 프리픽스가 없으면
// M1 으로 간주한다(단일 모터 시절의 호스트/스크립트 호환).
//
//   M2 START            구동 시작 (저속에서 목표까지 가속)          -> OK: STARTED
//   M2 STOP             정지 (홀딩 토크는 유지)                     -> OK: STOPPED
//   M2 SPEED <sps>      목표 속도를 steps/sec 로 지정               -> OK: TARGET_SPS=...
//   M2 RPM <rpm>        목표 속도를 모터축 회전수로 지정             -> OK: TARGET_RPM=...
//   M2 DIR CW|CCW       회전 방향 (구동 중이면 잠깐 멈추고 재출발)   -> OK: DIR=CW
//   M2 ENABLE|DISABLE   드라이버 활성/해제                          -> OK: ENABLED
//   M2 STATUS           해당 모터 상태를 JSON 한 줄로               -> {"running":true,...}
//   M3 ZERO             현재 위치를 원점(0)으로                      -> OK: ZERO
//   M3 LIMIT <rev>      가동 범위를 원점부터 <rev> 회전까지로 (0=해제) -> OK: LIMIT=12.00
//   M3 GOTO <rev>       그 위치까지 가서 스스로 멈춘다(방향 자동)     -> OK: GOTO 8.000
//   SERVO <deg>         분류 게이트 서보 각도 (0~180, 보드에 1개)    -> OK: SERVO=90
//   PING                생존 확인                                   -> PONG
//
// 펌웨어 재업로드: scripts/flash_conveyor.py (빌드·포트해제·플래시 자동).
// DFU 진입만 손으로 한다 — BOOT0 누른 채 NRST 눌렀다 떼고 BOOT0 떼기.
//
// LM 가이드 소프트 리밋 (ZERO/LIMIT)
//   리밋 스위치가 없으므로 위치는 '보낸 펄스 수'를 적산해 추정한다. 원점은 사람이
//   잡는다: 축을 한쪽 끝으로 옮기고 ZERO 를 보내면 거기가 0 이 된다. 그 뒤 범위를
//   벗어나려 하면 펌웨어가 스스로 멈춘다(리밋 밖으로 나가는 방향의 START 는 거부).
//   한계: 탈조하거나 손으로 밀면 추정 위치와 실제가 어긋난다 — 그때는 다시 ZERO.
//   전원을 껐다 켜면 위치는 0 으로 초기화되므로 원점을 다시 잡아야 한다.
//
// 응답 규칙: 성공은 "OK: ", 실패는 "ERR: " 로 시작한다. STATUS 만 '{' 로 시작하는
// JSON 한 줄이다. 호스트(backend/core/conveyor.py)가 이 접두사로 분기한다.

// 펄스는 loop() 안에서 digitalWrite 로 만든다. 하드웨어 타이머 PWM 이 이 리그에서
// 조용히 죽었기 때문이다(2026-08-14 실측: 명령은 OK 인데 세 축 모두 완전 무음.
// 같은 배선에서 digitalWrite 토글로는 축이 정상적으로 돌았다 — pin_finder 로 확인).
// 필요한 속도가 수천 sps 라 84MHz MCU 에게는 부담이 아니다. 타이머 PWM 은 MHz 급
// 펄스가 필요할 때의 최적화였는데, 여기서는 쓰지도 않을 성능 때문에 동작을 잃었다.
// ponytail: 소프트 펄스, 20k sps 이상이 필요해지면 그때 타이머를 다시 붙인다.
#include <Servo.h>

// 공통 애노드 배선(3.3V 마진 문제 시)으로 바꾸면 true 로. 신호가 반전된다.
const bool DIR_ACTIVE_LOW = false;
const bool ENA_ACTIVE_LOW = false;

// ── 모터/드라이버 제원 ────────────────────────────────────────────────
// 1.8도 모터 = 200 full step/rev. MICROSTEP 은 각 TB6600 DIP 스위치(S1~S3)와
// 반드시 일치시켜야 한다. 세 드라이버 모두 같은 DIP 설정을 쓴다는 전제다 —
// LM 가이드 쪽만 다르게 놓았다면 이 값을 모터별 배열로 쪼갠다.
const float MOTOR_STEPS_PER_REV = 200.0;
const float MICROSTEP           = 8;
const float STEPS_PER_REV       = MOTOR_STEPS_PER_REV * MICROSTEP;

// ── 속도 ──────────────────────────────────────────────────────────────
// 주의: 스텝모터는 1000RPM 부근부터 토크가 급락한다. 실제 한계는 부하 걸고 찾는다.
const float MIN_SPS = 10.0;
// 소프트 펄스의 현실적 상한. 3축 동시 20k sps = 초당 12만 번 토글인데, 그 이상은
// loop 주기가 반주기보다 길어져 속도가 부정확해진다(모터가 못 따라오는 영역이기도 하다).
const float MAX_SPS = 20000.0;
const float START_SPS = 400.0;            // 출발 속도(steps/sec)
const float RAMP_SPS_PER_SEC = 20000.0;   // 가속도. 8000sps 까지 약 0.4초
const unsigned long RAMP_UPDATE_US = 2000; // 램프 갱신 주기

// TB6600 은 방향 신호가 바뀐 뒤 최소 5us 뒤에 펄스가 와야 한다.
// 부하 걸린 상태에서 즉시 반전 = 탈조. 넉넉히 멈춘 뒤 재출발한다.
const unsigned long DIR_SETTLE_MS = 150;

// 소프트웨어 DFU 진입(1200bps 터치 / DFU 명령)은 껐다.
//
// 2026-08-14 실측: 점프 자체는 동작해서 CDC 가 사라지는 것까지는 확인됐지만,
// ROM 부트로더가 USB 로 다시 열거되지 않아 보드가 아예 안 보이는 상태가 됐다.
// 그때마다 BOOT0+NRST 로 복구해야 했으므로, '버튼을 안 눌러도 되게' 하려던 기능이
// 오히려 버튼을 더 누르게 만들었다. 업로드는 scripts/flash_conveyor.py 로 하고
// DFU 진입만 손으로 한다(BOOT0 누른 채 NRST).
//
// 다시 시도한다면: 여기서 바로 점프하지 말고 RTC 백업 레지스터에 플래그를 쓴 뒤
// NVIC_SystemReset() 으로 리셋하고, 부팅 맨 앞에서 그 플래그를 보고 점프할 것.
// 리셋을 거치면 USB 와 클럭이 깨끗한 상태에서 부트로더가 뜬다.
#define ENABLE_SOFT_DFU 0
const uint32_t DFU_TOUCH_BAUD = 1200;
// STM32F411 의 시스템 메모리(ROM 부트로더) 시작 주소. 칩마다 다르다.
const uint32_t SYSTEM_MEMORY_ADDR = 0x1FFF0000;

// ── 모터 3축 ──────────────────────────────────────────────────────────
struct Motor {
  uint32_t pulPin, dirPin, enaPin;
  bool running;
  bool enabled;
  bool dirCw;
  float currentSps;
  float targetSps;
  unsigned long lastRampUs;
  // 소프트 펄스 상태. halfPeriodUs 0 = 정지.
  unsigned long halfPeriodUs;
  unsigned long nextToggleUs;
  bool pulseHigh;
  // 소프트 리밋(LM 가이드용). 리밋 스위치가 없어 '보낸 펄스 수' 로 위치를 센다.
  // 소프트 펄스라 추정이 아니라 실제 카운트다. limitSteps 0 = 리밋 없음.
  long posSteps;
  long limitSteps;
  // GOTO: 목표 위치까지 가서 스스로 멈춘다. seeking 이 false 면 무한 주행
  // (컨베이어 벨트가 그렇다). 방향은 GOTO 가 현재 위치와 비교해 알아서 정한다.
  bool seeking;
  long seekTarget;
};

const int NUM_MOTORS = 3;
Motor motors[NUM_MOTORS] = {
  // pul, dir, ena,                                        pos,lim, seeking,target
  { PA0, PA1, PA2, false, true, true, START_SPS, 8000.0, 0, 0, 0, false, 0, 0, false, 0 },  // M1 컨베이어 1
  { PA3, PA6, PA7, false, true, true, START_SPS, 8000.0, 0, 0, 0, false, 0, 0, false, 0 },  // M2 컨베이어 2
  { PB0, PB1, PA8, false, true, true, START_SPS, 8000.0, 0, 0, 0, false, 0, 0, false, 0 },  // M3 LM 가이드
};

// 분류 게이트 서보(보드에 1개). PB3 을 Servo 라이브러리(전용 타이머 인터럽트)로 구동 —
// 하드웨어 PWM 은 모터 펄스 타이머들과 주파수를 공유할 수 없어서다.
const uint32_t SERVO_PIN = PB3;
Servo gate;
int servoDeg = 90;              // 부팅 기본값: 중립
// attach() 는 SERVO 명령이 처음 올 때까지 미룬다. Servo 라이브러리는 attach 하는
// 순간 타이머 하나를 통째로 점유하는데, 그게 모터 PUL 타이머와 겹치면 그 축의
// 펄스가 죽는다(증상: 명령은 OK 인데 모터가 완전히 무음). 게이트를 쓰지 않는
// 동안에는 잡지 않는 편이 안전하다 — 컨베이어가 서보보다 중요하다.
bool gateAttached = false;

String lineBuf;

// ── 헬퍼 ──────────────────────────────────────────────────────────────
void setPulseHz(Motor &m, float hz) {
  if (hz < 1.0) {
    m.halfPeriodUs = 0;             // loop 가 이 축의 토글을 건너뛴다
    digitalWrite(m.pulPin, LOW);    // 확정 LOW 로 파킹
    m.pulseHigh = false;
    return;
  }
  // 듀티 50%. TB6600 은 엣지만 보므로 반주기만 맞으면 된다.
  m.halfPeriodUs = (unsigned long)(500000.0f / hz);
  if (m.halfPeriodUs == 0) m.halfPeriodUs = 1;
  m.nextToggleUs = micros() + m.halfPeriodUs;
}

void applyDir(Motor &m) {
  bool level = m.dirCw ? LOW : HIGH;      // 진행 방향이 반대면 이 두 값을 바꾼다
  if (DIR_ACTIVE_LOW) level = !level;
  digitalWrite(m.dirPin, level);
}

void applyEnable(Motor &m) {
  bool level = m.enabled ? LOW : HIGH;    // LOW = 드라이버 활성화 (공통 GND 기준)
  if (ENA_ACTIVE_LOW) level = !level;
  digitalWrite(m.enaPin, level);
}

// 소프트 리밋이 걸린 축(LM 가이드)이 지금 방향으로 더 갈 수 있는가.
// 리밋이 0 이면 제한 없음 — 컨베이어 벨트는 항상 여기서 true 다.
bool canMove(const Motor &m) {
  if (m.limitSteps <= 0) return true;
  return m.dirCw ? (m.posSteps < m.limitSteps) : (m.posSteps > 0);
}

void beginRun(Motor &m) {
  m.running = true;
  m.currentSps = START_SPS;    // 매번 저속에서 다시 출발
  m.lastRampUs = micros();
  if (!m.enabled) { m.enabled = true; applyEnable(m); }
  setPulseHz(m, m.currentSps);
}

void endRun(Motor &m) {
  m.running = false;
  m.seeking = false;    // 목표 주행 중이었다면 그것도 끝난다
  setPulseHz(m, 0);
}

#if ENABLE_SOFT_DFU
// ROM 부트로더로 넘어간다(1200bps 터치 또는 DFU 명령). 돌아오지 않는다.
// 주의: 이 구현은 실기에서 부트로더가 USB 로 다시 열거되지 않았다(위 주석 참조).
void jumpToBootloader() {
  __disable_irq();

  USB_OTG_DeviceTypeDef *usbDev =
      (USB_OTG_DeviceTypeDef *)((uint32_t)USB_OTG_FS + USB_OTG_DEVICE_BASE);
  usbDev->DCTL |= USB_OTG_DCTL_SDIS;        // soft disconnect
  for (volatile uint32_t i = 0; i < 2000000; i++) { }   // 호스트가 분리를 인지할 시간

  HAL_RCC_DeInit();
  HAL_DeInit();
  SysTick->CTRL = 0;
  SysTick->LOAD = 0;
  SysTick->VAL = 0;

  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_SYSCFG_REMAPMEMORY_SYSTEMFLASH();   // 벡터 테이블을 시스템 메모리로

  void (*bootJump)(void) = (void (*)(void))(*(volatile uint32_t *)(SYSTEM_MEMORY_ADDR + 4));
  __set_MSP(*(volatile uint32_t *)SYSTEM_MEMORY_ADDR);
  bootJump();
}

// 부트로더로 가기 전에 반드시 세 축을 세운다. 돌던 모터를 그대로 두고 넘어가면
// 펌웨어를 올리는 내내 벨트가 돈다(펄스는 멈추지만 상태가 불확실해진다).
void enterDfu() {
  for (int i = 0; i < NUM_MOTORS; i++) endRun(motors[i]);
  jumpToBootloader();
}
#endif  // ENABLE_SOFT_DFU

float spsToRpm(float sps) { return sps * 60.0 / STEPS_PER_REV; }
float rpmToSps(float rpm) { return rpm * STEPS_PER_REV / 60.0; }

void printStatus(int mi) {
  Motor &m = motors[mi];
  // JSON 한 줄. 키를 늘릴 때 호스트의 status() 파서도 같이 본다.
  Serial.print("{\"motor\":");     Serial.print(mi + 1);
  Serial.print(",\"running\":");   Serial.print(m.running ? "true" : "false");
  Serial.print(",\"enabled\":");   Serial.print(m.enabled ? "true" : "false");
  Serial.print(",\"dir\":\"");     Serial.print(m.dirCw ? "cw" : "ccw");
  Serial.print("\",\"sps\":");     Serial.print(m.running ? m.currentSps : 0.0, 1);
  Serial.print(",\"target_sps\":");Serial.print(m.targetSps, 1);
  Serial.print(",\"rpm\":");       Serial.print(spsToRpm(m.running ? m.currentSps : 0.0), 2);
  Serial.print(",\"target_rpm\":");Serial.print(spsToRpm(m.targetSps), 2);
  Serial.print(",\"microstep\":"); Serial.print((int)MICROSTEP);
  Serial.print(",\"steps_per_rev\":"); Serial.print((int)STEPS_PER_REV);
  if (m.limitSteps > 0) {
    // 소프트 리밋을 쓰는 축만 위치를 싣는다 — 대시보드가 이 키의 유무로
    // '원점/리밋 UI 를 붙일 축'을 고른다(벨트에는 안 붙는다).
    Serial.print(",\"pos_rev\":");   Serial.print(m.posSteps / STEPS_PER_REV, 3);
    Serial.print(",\"limit_rev\":"); Serial.print(m.limitSteps / STEPS_PER_REV, 3);
  }
  if (mi == 0) {
    // 서보는 보드에 1개다. M1 상태에만 실어 게이트 UI 가 한 카드에만 붙게 한다.
    Serial.print(",\"servo_deg\":"); Serial.print(servoDeg);
  }
  Serial.println("}");
}

void handleCommand(int mi, const String& cmd) {
  Motor &m = motors[mi];

  if (cmd == "START") {
    // 이미 리밋 끝에 서 있으면 그 방향으로는 출발시키지 않는다.
    // (반대 방향으로 DIR 을 바꾼 뒤 START 하면 빠져나올 수 있다)
    if (!canMove(m)) {
      Serial.print("ERR: 소프트 리밋 끝이다 (위치 ");
      Serial.print(m.posSteps / STEPS_PER_REV, 2);
      Serial.print("/"); Serial.print(m.limitSteps / STEPS_PER_REV, 2);
      Serial.println(" rev). 방향을 바꾸거나 ZERO 로 원점을 다시 잡을 것.");
      return;
    }
    beginRun(m);
    Serial.println("OK: STARTED");

  } else if (cmd == "STOP") {
    endRun(m);
    Serial.println("OK: STOPPED");

  } else if (cmd.startsWith("SPEED ")) {
    float v = cmd.substring(6).toFloat();
    if (v >= MIN_SPS && v <= MAX_SPS) {
      m.targetSps = v;
      Serial.print("OK: TARGET_SPS="); Serial.println(m.targetSps, 1);
    } else {
      Serial.print("ERR: SPEED 범위는 "); Serial.print(MIN_SPS, 0);
      Serial.print("~"); Serial.println(MAX_SPS, 0);
    }

  } else if (cmd.startsWith("RPM ")) {
    float sps = rpmToSps(cmd.substring(4).toFloat());
    if (sps >= MIN_SPS && sps <= MAX_SPS) {
      m.targetSps = sps;
      Serial.print("OK: TARGET_RPM="); Serial.println(spsToRpm(m.targetSps), 2);
    } else {
      Serial.print("ERR: RPM 범위는 "); Serial.print(spsToRpm(MIN_SPS), 2);
      Serial.print("~"); Serial.println(spsToRpm(MAX_SPS), 2);
    }

  } else if (cmd.startsWith("DIR ")) {
    String arg = cmd.substring(4);
    arg.trim();
    arg.toUpperCase();
    bool wantCw;
    if (arg == "CW" || arg == "1")       wantCw = true;
    else if (arg == "CCW" || arg == "0") wantCw = false;
    else { Serial.println("ERR: DIR 인자는 CW 또는 CCW"); return; }

    if (wantCw != m.dirCw) {
      bool wasRunning = m.running;
      if (wasRunning) endRun(m);   // 부하 걸린 상태에서 즉시 반전 = 탈조
      m.dirCw = wantCw;
      applyDir(m);
      if (wasRunning) {
        delay(DIR_SETTLE_MS);      // 멎을 시간
        beginRun(m);               // 저속에서 다시 가속
      }
    }
    Serial.print("OK: DIR="); Serial.println(m.dirCw ? "CW" : "CCW");

  } else if (cmd == "ENABLE") {
    m.enabled = true; applyEnable(m);
    Serial.println("OK: ENABLED");

  } else if (cmd == "DISABLE") {
    // 드라이버를 놓으면 홀딩 토크가 사라져 축을 손으로 움직일 수 있다.
    endRun(m);
    m.enabled = false; applyEnable(m);
    Serial.println("OK: DISABLED");

  } else if (cmd.startsWith("SERVO ")) {
    // 분류 게이트 각도(보드에 1개라 프리픽스와 무관하게 같은 서보다). 예: SERVO 45
    int deg = cmd.substring(6).toInt();
    if (deg < 0 || deg > 180) {
      Serial.println("ERR: SERVO 각도는 0~180");
    } else {
      if (!gateAttached) { gate.attach(SERVO_PIN); gateAttached = true; }
      servoDeg = deg;
      gate.write(servoDeg);
      Serial.print("OK: SERVO="); Serial.println(servoDeg);
    }

  } else if (cmd.startsWith("GOTO ")) {
    // 목표 위치(회전수)까지 가서 스스로 멈춘다. 방향은 현재 위치와 비교해 정한다.
    // 리밋이 있으면 그 안으로 잘라낸다 — 범위 밖을 찍어도 끝에서 멎을 뿐이다.
    long target = (long)(cmd.substring(5).toFloat() * STEPS_PER_REV);
    if (m.limitSteps > 0) {
      if (target < 0) target = 0;
      if (target > m.limitSteps) target = m.limitSteps;
    }
    if (target == m.posSteps) {
      Serial.println("OK: GOTO 이미 그 위치다");
      return;
    }
    bool wantCw = target > m.posSteps;
    if (wantCw != m.dirCw) {
      if (m.running) { endRun(m); delay(DIR_SETTLE_MS); }   // 부하 상태 즉시 반전 = 탈조
      m.dirCw = wantCw;
      applyDir(m);
    }
    m.seekTarget = target;
    beginRun(m);
    m.seeking = true;    // beginRun 이 endRun 을 거칠 수 있으므로 그 뒤에 세운다
    Serial.print("OK: GOTO "); Serial.println((float)target / STEPS_PER_REV, 3);

  } else if (cmd == "ZERO") {
    // 리밋 스위치가 없으니 원점은 사람이 잡는다: 축을 한쪽 끝에 두고 이 명령.
    m.posSteps = 0;
    Serial.println("OK: ZERO");

  } else if (cmd.startsWith("LIMIT ")) {
    // 가동 범위를 원점부터 <rev> 회전까지로. 0 이면 리밋 해제(무한 회전).
    float rev = cmd.substring(6).toFloat();
    if (rev < 0) {
      Serial.println("ERR: LIMIT 은 0 이상 (0 = 해제)");
    } else {
      m.limitSteps = rev * STEPS_PER_REV;
      Serial.print("OK: LIMIT="); Serial.println(rev, 2);
    }

  } else if (cmd == "STATUS") {
    printStatus(mi);

#if ENABLE_SOFT_DFU
  } else if (cmd == "DFU") {
    // 펌웨어 재업로드용. 응답을 먼저 내보내고 넘어간다(넘어가면 포트가 사라진다).
    Serial.println("OK: DFU");
    Serial.flush();
    delay(50);
    enterDfu();
#endif

  } else if (cmd == "PING") {
    Serial.println("PONG");

  } else {
    Serial.print("ERR: UNKNOWN COMMAND: ");
    Serial.println(cmd);
  }
}

// "M2 START" → 모터 인덱스 1 + "START". 프리픽스가 없으면 M1(호환).
void dispatch(String cmd) {
  int mi = 0;
  if (cmd.length() >= 3 && cmd[0] == 'M' &&
      cmd[1] >= '1' && cmd[1] < '1' + NUM_MOTORS && cmd[2] == ' ') {
    mi = cmd[1] - '1';
    cmd = cmd.substring(3);
    cmd.trim();
  }
  handleCommand(mi, cmd);
}

void setup() {
  for (int i = 0; i < NUM_MOTORS; i++) {
    Motor &m = motors[i];
    pinMode(m.pulPin, OUTPUT);
    pinMode(m.dirPin, OUTPUT);
    pinMode(m.enaPin, OUTPUT);
    setPulseHz(m, 0);
    applyDir(m);
    applyEnable(m);   // 활성화 (홀딩 토크 유지)
  }

  // 서보는 여기서 attach 하지 않는다(gateAttached 주석 참조). SERVO 명령이 올 때 잡는다.

  lineBuf.reserve(32);
  Serial.begin(115200);   // USB CDC 는 baud 를 무시하지만 관례상 둔다
  delay(300);
  // 네이티브 USB 라 포트를 열어도 리셋되지 않는다. 이 배너는 '부팅 직후 호스트가
  // 이미 듣고 있던 경우'에만 보이고, 호스트는 배너가 없어도 진행한다(_wait_boot).
  Serial.println("READY: 3-Axis Stepper Controller Initialized (BlackPill F411, soft pulse)");
}

void loop() {
#if ENABLE_SOFT_DFU
  // 호스트가 1200bps 로 포트를 열면 '펌웨어를 올리겠다' 는 신호다(아두이노 관례).
  // 우리 호스트는 115200 만 쓰므로 평소에 걸리지 않는다.
  if (Serial.baud() == DFU_TOUCH_BAUD) enterDfu();
#endif

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      lineBuf.trim();
      if (lineBuf.length() > 0) dispatch(lineBuf);
      lineBuf = "";
    } else if (c != '\r') {
      lineBuf += c;
    }
  }

  unsigned long now = micros();

  // ── 펄스 생성 ──
  // 이 블록이 실제로 모터를 돌린다. 매 loop 마다 돌므로 안에서 시간을 끄는 일
  // (Serial 출력, delay)을 하면 그대로 속도 오차가 된다.
  for (int i = 0; i < NUM_MOTORS; i++) {
    Motor &m = motors[i];
    if (!m.running || m.halfPeriodUs == 0) continue;
    // 부호 있는 비교라야 micros() 가 한 바퀴 돌 때(약 71분) 멈추지 않는다.
    if ((long)(now - m.nextToggleUs) < 0) continue;

    m.pulseHigh = !m.pulseHigh;
    digitalWrite(m.pulPin, m.pulseHigh ? HIGH : LOW);
    m.nextToggleUs = now + m.halfPeriodUs;

    if (!m.pulseHigh) {
      // 하강 엣지 = 한 스텝 완료. 추정이 아니라 실제로 보낸 펄스를 센다.
      m.posSteps += m.dirCw ? 1 : -1;
      // GOTO 목표 도달 — 지나치지 않게 매 스텝 확인한다.
      if (m.seeking &&
          (m.dirCw ? m.posSteps >= m.seekTarget : m.posSteps <= m.seekTarget)) {
        m.posSteps = m.seekTarget;
        endRun(m);
      } else if (m.limitSteps > 0 && !canMove(m)) {
        endRun(m);                                    // 소프트 리밋 끝
        if (m.posSteps < 0) m.posSteps = 0;
        if (m.posSteps > m.limitSteps) m.posSteps = m.limitSteps;
      }
    }
  }

  // ── 가속 램프 ──
  for (int i = 0; i < NUM_MOTORS; i++) {
    Motor &m = motors[i];
    if (!m.running) continue;
    if (now - m.lastRampUs < RAMP_UPDATE_US) continue;   // 2ms 마다만 갱신

    float dt = (now - m.lastRampUs) / 1000000.0f;
    m.lastRampUs = now;

    // 가속은 서서히, 감속은 즉시(느려지는 방향은 탈조 위험이 없다).
    if (m.currentSps < m.targetSps) {
      m.currentSps += RAMP_SPS_PER_SEC * dt;
      if (m.currentSps > m.targetSps) m.currentSps = m.targetSps;
    } else if (m.currentSps > m.targetSps) {
      m.currentSps = m.targetSps;
    } else {
      continue;                    // 목표 속도 도달 — 반주기를 다시 계산할 필요 없다
    }
    // 반주기만 갱신한다. setPulseHz 를 부르면 nextToggleUs 가 매번 밀려 펄스가 끊긴다.
    m.halfPeriodUs = (unsigned long)(500000.0f / m.currentSps);
    if (m.halfPeriodUs == 0) m.halfPeriodUs = 1;
  }
}
