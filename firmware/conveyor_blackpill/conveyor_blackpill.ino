// STM32F411 "BlackPill" (DFRobot DFR0864) + HW-095 + DC 모터 ×2: 컨베이어 2축 UART 제어
//
//   M1 = 컨베이어 벨트 1 (공급, feed)          = 드라이버 A 채널
//   M2 = 컨베이어 벨트 2 (로봇 작업 영역, work)  = 드라이버 B 채널
//   LM 가이드(구 M3)는 하드웨어에서 사라졌다. "M3 ..." 는 ERR: NO MOTOR 3 이다.
//   구동부가 스텝모터(TB6600 ×3, 소프트 펄스)에서 DC 모터로 바뀌었다. 엔코더가 없는 모터로 보므로
//   RPM·위치는 알 수 없고, 속도는 PWM 듀티(%)로만 정한다.
//
// 드라이버 HW-095
//   L298N 듀얼 H-브리지 모듈로 추정한다. 'HW-095' 라는 이름이 L298N 인지는 확인하지 못했다
//   (판매처 표기로는 'HW-095A' 가 L298 기반으로 나온다). 실물 칩 마킹으로 확인할 것.
//   아래 동작 근거는 ST L298 데이터시트(13쪽본)의 전기적 특성·진리표다.
//   채널마다 ENx(속도, PWM)와 INx 2개(방향)를 쓴다.
//     EN=H : IN1=H,IN2=L 정방향(CW) / IN1=L,IN2=H 역방향(CCW) / IN1=IN2 브레이크(Fast Motor Stop)
//     EN=L : IN 과 무관하게 프리런(Free Running Motor Stop)
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
// 배선 (BlackPill 쪽. 기존 TB6600 배선 핀을 재사용했다. PB0 는 구 LM 가이드의 PUL 자리)
//   M1 (A 채널): ENA → PB0 (PB0_ALT1 = TIM3_CH3),     IN1 → PA1, IN2 → PA2
//   M2 (B 채널): ENB → PA3 (PA3_ALT1 = TIM5_CH4),     IN3 → PA6, IN4 → PA7
//   분류 게이트 서보 신호선 → PB3 (서보 전원은 외부 5V — 스톨 전류가 보드 한계를 넘는다)
//   ⚠ PA0 는 쓰지 않는다. 온보드 KEY 버튼이라 누르면 GND 에 직결된다(core 의 variant 헤더에도
//     USER_BTN = PA0). 출력(PWM)으로 쓰면 버튼을 누를 때 핀이 손상될 수 있어 펌웨어는 PA0 을
//     건드리지 않는다(리셋 상태의 입력 그대로).
//
//   ENx 는 analogWrite 하드웨어 PWM 이다. 서로 다른 타이머(TIM3/TIM5)여야 하고, Servo 라이브러리가
//   쓰는 타이머와도 겹치면 안 된다. 서보 타이머 근거(STM32duino core 3.0.0 소스에서 확인):
//   variants/STM32F4xx/F411C(C-E)(U-Y)/variant_BLACKPILL_F411CE.h 가 TIMER_SERVO = TIM11
//   (TIMER_TONE = TIM10)으로 정하고, libraries/Servo/src/stm32/Servo.cpp 가 그 타이머의 업데이트
//   인터럽트에서 digitalWrite 로 서보 펄스를 만든다(PB3 에 하드웨어 PWM 을 쓰지 않는다).
//   TIM11 은 TIM3/TIM5 와 겹치지 않는다.
//   함정: 이 보드의 핀맵(PeripheralPins_BLACKPILL_F411CE.c)에서 PB0 의 1순위는 TIM1_CH2N, PA3 의 1순위는
//   TIM2_CH4 다. TIM3_CH3 / TIM5_CH4 는 대체 기능이라 PB0_ALT1 / PA3_ALT1 로 지정해야 잡힌다.
//   setup() 이 이 겹침(과 PWM 타이머가 없는 핀)을 검사해 걸리면 ERR 배너를 뱉고 START 를 거부한다.
//
// 전원
//   - 모터 전원 12V 를 모듈의 12V 단자에 넣는다. GND 는 BlackPill 과 반드시 공통으로 묶는다.
//   - 12V 이하면 모듈의 5V 레귤레이터 점퍼를 그대로 둔다.
//   - ENA/ENB 점퍼는 반드시 뽑는다. 꽂혀 있으면 EN 이 5V 에 묶여 항상 전속이라 PWM 이 안 먹고,
//     보드 핀(EN)과 출력끼리 충돌한다.
//   - 브레이크 전류는 L298 데이터시트(7쪽)상 절대 최대 2A 를 넘으면 안 된다. 큰 모터를 달면 확인할 것.
//
// 3.3V 로직 마진
//   BlackPill 출력은 3.3V 다. L298 입력(IN, EN)의 High 하한은 2.3V, Low 상한은 1.5V
//   (데이터시트 ViH 2.3V~VSS, ViL -0.3~1.5V, VSS 4.5~7V)라서 3.3V 직결 구동이 가능하고 High 쪽
//   여유는 1.0V 다. 레벨시프터는 쓰지 않는다. 모듈 회로(입력단 부품 유무)는 확인하지 못했으므로
//   실기에서 EN/IN 이 실제로 먹는지 확인할 것.
//
// PC(Flask)가 USB(CDC)로 한 줄 명령을 보내면 한 줄로 응답한다.
// 모든 명령 앞에 "M1 "/"M2 " 를 붙여 모터를 지정한다. 프리픽스가 없으면
// M1 으로 간주한다(단일 모터 시절의 호스트/스크립트 호환).
//
//   M1 START            설정된 방향·듀티로 구동(RAMP_MS 동안 램프 가속)   -> OK: RUN | ERR: DISABLED
//   M1 STOP             브레이크 정지(EN=H, IN1=IN2=L)                    -> OK: STOP
//   M1 DUTY <pct>       목표 듀티 0~100(실수). 구동 중이면 램프로 반영.     -> OK: DUTY=<pct>
//                       0 이면 STOP 과 같다. MIN_RUN_DUTY 미만의 0 이 아닌     | ERR: DUTY 범위는 0~100
//                       값은 MIN_RUN_DUTY 로 올리고 응답에 그 값을 적는다.
//   M1 DIR CW|CCW       회전 방향. 구동 중이면 거부                        -> OK: DIR=CW | ERR: RUNNING
//   M1 ENABLE           드라이버 허용(아직 돌지는 않는다)                  -> OK: ENABLED
//   M1 DISABLE          EN=L, IN=L 프리런 정지. ENABLE 전까지 START 거부    -> OK: DISABLED
//   M1 STATUS           이 모터 상태 한 줄(DUTY 는 설정값)                 -> OK: STATUS RUN=<0|1> DIR=<CW|CCW>
//                                                                              DUTY=<pct> EN=<0|1> SERVO=<deg>
//   SERVO <deg>         분류 게이트 서보 각도 (0~180, 보드에 1개)          -> OK: SERVO=<deg>
//   PING                생존 확인. 호스트 워치독 갱신에도 쓴다              -> OK: PONG
//   DFU                 부트로더 진입(ENABLE_SOFT_DFU 1 일 때만. 기본 꺼짐)  -> OK: DFU
//   그 밖의 명령은 ERR: UNKNOWN COMMAND: <cmd>, 없는 모터는 ERR: NO MOTOR <n>.
//   (핀·타이머 검사에 걸렸으면 START 가 ERR: TIMER CONFLICT)
//
// 부팅 상태: 모든 모터 정지(브레이크), enabled=true, 방향 CW, 목표 듀티 DEFAULT_DUTY.
//
// 호스트 워치독 (안전)
//   어떤 모터든 달리는 중에 HOST_TIMEOUT_MS 넘게 어떤 줄도 받지 못하면 모든 모터를 STOP(브레이크)하고
//   "WARN: HOST TIMEOUT" 을 한 번 낸다. 서버가 사이클 도중 죽어도 벨트가 계속 돌지 않게 하려는 것이다.
//   명령이든 PING 이든 줄을 하나라도 받으면 타이머가 갱신된다 — 그래서 호스트는 벨트가 하나라도 도는
//   동안 같은 포트로 0.5초마다 PING 을 보내야 한다. USB CDC 의 DTR 이 내려가면(호스트가 포트를
//   닫음) 2초를 기다리지 않고 같은 경로로 바로 선다.
//
// 응답 규칙: 한 줄, 성공은 "OK: ", 실패는 "ERR: " 로 시작한다. "READY:"(부팅)와 "WARN:"(워치독)은
// 명령과 무관하게 불쑥 나올 수 있는 알림 줄이다 — 호스트는 응답을 기다리는 중에 이 줄들을 건너뛰어야 한다.

#include <Servo.h>
#include "PeripheralPins.h"   // PinMap_TIM, pinmap_peripheral — setup() 의 타이머 겹침 검사용

// ── 현장 튜닝 상수 ────────────────────────────────────────────────────
// 종이 위 숫자가 아니라 실기에서 맞춰야 하는 값들이다.
constexpr float DEFAULT_DUTY = 40.0f;   // 부팅 시 목표 듀티(%)
// 모터가 실제로 도는 최저 듀티(%). 이보다 낮은 0 이 아닌 DUTY 는 이 값으로 올린다 — 전류만 흐르고
// 안 도는 구간을 막는다. ⚠ 20 은 실측 전 임시값이다. 벨트를 얹은 채 서서히 올려 재서 교체할 것.
constexpr float MIN_RUN_DUTY = 20.0f;
// PWM 주파수. L298 데이터시트의 스위칭 주파수 fc 는 25~40kHz(IL=2A)라 이 값은 한참 아래다.
// 가청 소음이 거슬리면 올리고, 저속 토크가 모자라거나 모듈이 뜨거우면 내린다(실측).
constexpr uint32_t PWM_HZ = 2000;
constexpr int PWM_BITS = 10;                        // analogWrite 해상도: 듀티 0~1023
constexpr int PWM_MAX = (1 << PWM_BITS) - 1;
// 0 → 목표 듀티까지 올리는 시간. 부품이 넘어지지 않게 부드럽게 출발한다. 내릴 때는 즉시 내린다.
constexpr unsigned long RAMP_MS = 300;
// 호스트가 이 시간(ms) 넘게 아무 줄도 안 보내면 모든 모터를 세운다(워치독).
constexpr unsigned long HOST_TIMEOUT_MS = 2000;
static_assert(DEFAULT_DUTY >= MIN_RUN_DUTY, "DEFAULT_DUTY < MIN_RUN_DUTY 면 START 해도 벨트가 안 돈다");

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

// ── 모터 2축 ──────────────────────────────────────────────────────────
struct Motor {
  uint32_t enPin, in1, in2;
  float dutyTarget;   // 설정한 목표 듀티(%). START·DUTY 가 이 값을 향해 램프한다. STATUS 의 DUTY.
  float dutyNow;      // 지금 EN 에 나가는 듀티(%). 구동 중에만 의미가 있다.
  bool dirCw;         // CW = IN1=H, IN2=L. 실물 진행 방향과 반대면 모터 선을 바꾸거나 호스트에서 뒤집는다.
  bool running;
  bool enabled;       // false = 드라이버 해제(프리런). START 를 거부한다.
};

const int NUM_MOTORS = 2;
Motor motors[NUM_MOTORS] = {
  // en,       in1, in2, dutyTarget,   dutyNow, dirCw, running, enabled
  { PB0_ALT1,  PA1, PA2, DEFAULT_DUTY, 0, true, false, true },  // M1 feed (A 채널, TIM3_CH3)
  { PA3_ALT1,  PA6, PA7, DEFAULT_DUTY, 0, true, false, true },  // M2 work (B 채널, TIM5_CH4)
};

// 분류 게이트 서보(보드에 1개). PB3 을 Servo 라이브러리(전용 타이머 인터럽트)로 구동한다 —
// 타이머는 TIM11 이고 ENx 의 TIM3/TIM5 와 겹치지 않는다(위 배선 주석, setup() 검사).
const uint32_t SERVO_PIN = PB3;
Servo gate;
int servoDeg = 90;              // 부팅 기본값: 중립
// attach() 는 SERVO 명령이 처음 올 때까지 미룬다. attach 하는 순간 서보 타이머를 통째로
// 점유하므로, 겹치면 벨트 PWM 이 죽는다(증상: 명령은 OK 인데 모터가 완전히 무음). 게이트를
// 쓰지 않는 동안에는 잡지 않는 편이 안전하다 — 컨베이어가 서보보다 중요하다.
bool gateAttached = false;

String lineBuf;
unsigned long lastLineMs = 0;   // 마지막으로 줄을 받은 시각(호스트 워치독)
unsigned long lastRampMs = 0;   // 램프를 마지막으로 갱신한 시각
bool pwmConflict = false;       // setup() 의 핀·타이머 검사 결과

// ── 핀·타이머 검사 ────────────────────────────────────────────────────
// ENx 핀의 PWM 타이머. 타이머가 없는 핀이면 nullptr — analogWrite 가 조용히 on/off 디지털
// 출력으로 떨어지는 핀이다.
TIM_TypeDef *timerOf(uint32_t pin) {
  return (TIM_TypeDef *)pinmap_peripheral(digitalPinToPinName(pin), PinMap_TIM);
}

// ENx 가 모두 PWM 타이머를 갖고, 서로·서보 타이머(TIMER_SERVO)와 겹치지 않는가.
bool pwmPinsOk() {
  for (int i = 0; i < NUM_MOTORS; i++) {
    TIM_TypeDef *t = timerOf(motors[i].enPin);
    if (t == nullptr || t == TIMER_SERVO) return false;
    for (int j = 0; j < i; j++) {
      if (timerOf(motors[j].enPin) == t) return false;
    }
  }
  return true;
}

// ── 헬퍼 ──────────────────────────────────────────────────────────────
// EN 핀에 듀티(%)를 낸다. 0 = 항상 LOW, 100 = 항상 HIGH(PWM_MAX 에서 CCR = ARR+1).
// 하드웨어 PWM 은 한 번 걸면 analogWrite 로 듀티만 바꿔도 카운터를 리셋하지 않는다.
void setEn(const Motor &m, float pct) {
  analogWrite(m.enPin, (int)(pct * PWM_MAX / 100.0f + 0.5f));
}

// 0% 에서 출발. 순서가 중요하다: 브레이크(EN=H) 상태에서 IN 을 먼저 바꾸면 순간 전속으로 구동된다.
// EN 을 먼저 내린다(CCR 은 다음 PWM 주기 경계에 반영돼 최대 한 주기, 0.5ms 가 걸린다).
void beginRun(Motor &m) {
  setEn(m, 0);
  digitalWrite(m.in1, m.dirCw ? HIGH : LOW);
  digitalWrite(m.in2, m.dirCw ? LOW : HIGH);
  m.dutyNow = 0;
  m.running = true;
}

// 정지. 활성 상태면 브레이크(IN1=IN2=L 을 먼저, 그다음 EN=H), 해제 상태면 프리런(EN=L, IN=L).
// ponytail: 감속 램프 없이 즉시 브레이크 — 부품이 넘어지면 정지에도 감속 램프를 넣는다.
void stopMotor(Motor &m) {
  m.running = false;
  m.dutyNow = 0;
  digitalWrite(m.in1, LOW);
  digitalWrite(m.in2, LOW);
  setEn(m, m.enabled ? 100 : 0);
}

void stopAll() {
  for (int i = 0; i < NUM_MOTORS; i++) stopMotor(motors[i]);
}

bool anyRunning() {
  for (int i = 0; i < NUM_MOTORS; i++) {
    if (motors[i].running) return true;
  }
  return false;
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

// 부트로더로 가기 전에 반드시 모든 모터를 세운다. 돌던 모터를 그대로 두고 넘어가면
// 펌웨어를 올리는 내내 상태가 불확실해진다.
void enterDfu() {
  stopAll();
  jumpToBootloader();
}
#endif  // ENABLE_SOFT_DFU

// 램프: 구동 중인 모터의 dutyNow 를 dutyTarget 쪽으로 옮긴다. millis() 기반이라 블로킹이 없다.
// 올릴 때는 0 → 목표가 RAMP_MS 걸리는 기울기(목표 × 경과ms / RAMP_MS)로, 내릴 때는 즉시 내린다.
// ponytail: 선형 램프, 0% 에서 출발 — 저듀티 구간(MIN_RUN_DUTY 미만)에서 모터가 헛돌면 출발 듀티를
// MIN_RUN_DUTY 로 올린다.
void rampTick() {
  unsigned long now = millis();
  unsigned long dt = now - lastRampMs;
  if (dt == 0) return;            // 같은 ms 안에서는 갱신하지 않는다
  lastRampMs = now;

  for (int i = 0; i < NUM_MOTORS; i++) {
    Motor &m = motors[i];
    if (!m.running || m.dutyNow == m.dutyTarget) continue;
    if (m.dutyNow > m.dutyTarget) {
      m.dutyNow = m.dutyTarget;
    } else {
      m.dutyNow += m.dutyTarget * dt / RAMP_MS;
      if (m.dutyNow > m.dutyTarget) m.dutyNow = m.dutyTarget;
    }
    setEn(m, m.dutyNow);
  }
}

void printStatus(const Motor &m) {
  // 한 줄. 호스트(backend/core/conveyor.py)가 이 형식을 파싱하므로 키·순서를 바꾸면 호스트도 같이 본다.
  // DUTY 는 설정(목표) 듀티다. 램프 중인 현재값이 아니다.
  Serial.print("OK: STATUS RUN=");  Serial.print(m.running ? 1 : 0);
  Serial.print(" DIR=");            Serial.print(m.dirCw ? "CW" : "CCW");
  Serial.print(" DUTY=");           Serial.print(m.dutyTarget, 1);
  Serial.print(" EN=");             Serial.print(m.enabled ? 1 : 0);
  Serial.print(" SERVO=");          Serial.println(servoDeg);
}

void handleCommand(int mi, const String& cmd) {
  Motor &m = motors[mi];

  if (cmd == "START") {
    if (!m.enabled) { Serial.println("ERR: DISABLED"); return; }
    if (pwmConflict) { Serial.println("ERR: TIMER CONFLICT"); return; }
    if (!m.running) beginRun(m);   // 이미 달리는 중이면 램프를 다시 시작하지 않는다
    Serial.println("OK: RUN");

  } else if (cmd == "STOP") {
    stopMotor(m);
    Serial.println("OK: STOP");

  } else if (cmd.startsWith("DUTY ")) {
    // 숫자가 아닌 인자를 0(=정지)으로 받아들이지 않도록 strtod 로 끝까지 검사한다.
    const char *arg = cmd.c_str() + 5;
    char *end;
    double v = strtod(arg, &end);
    if (end == arg || *end != '\0' || !(v >= 0.0 && v <= 100.0)) {
      Serial.println("ERR: DUTY 범위는 0~100");
      return;
    }
    float pct = (float)v;
    if (pct == 0) {
      stopMotor(m);                       // 0 이면 STOP 과 같다. 저장된 목표 듀티는 그대로 둔다
    } else {
      if (pct < MIN_RUN_DUTY) pct = MIN_RUN_DUTY;
      m.dutyTarget = pct;                 // 구동 중이면 rampTick 이 올리거나(램프) 내린다(즉시)
    }
    Serial.print("OK: DUTY="); Serial.println(pct, 1);

  } else if (cmd.startsWith("DIR ")) {
    String arg = cmd.substring(4);
    arg.trim();
    arg.toUpperCase();
    bool wantCw;
    if (arg == "CW" || arg == "1")       wantCw = true;
    else if (arg == "CCW" || arg == "0") wantCw = false;
    else { Serial.println("ERR: DIR 인자는 CW 또는 CCW"); return; }

    // 달리는 중 반전은 거부한다. 부하 걸린 상태에서 즉시 반전하면 모터·부품에 충격이다.
    if (m.running) { Serial.println("ERR: RUNNING"); return; }
    m.dirCw = wantCw;                     // IN 핀은 START 가 방향을 읽어 건다(정지 중에는 브레이크 유지)
    Serial.print("OK: DIR="); Serial.println(m.dirCw ? "CW" : "CCW");

  } else if (cmd == "ENABLE") {
    // 풀려 있던 드라이버를 브레이크 상태(부팅 상태와 같다)로 되돌린다. 아직 돌지는 않는다.
    if (!m.enabled) { m.enabled = true; stopMotor(m); }
    Serial.println("OK: ENABLED");

  } else if (cmd == "DISABLE") {
    // EN=L, IN=L 프리런. 토크가 사라져 벨트를 손으로 움직일 수 있다.
    m.enabled = false;
    stopMotor(m);                         // enabled=false 이므로 브레이크가 아니라 프리런
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

  } else if (cmd == "STATUS") {
    printStatus(m);

#if ENABLE_SOFT_DFU
  } else if (cmd == "DFU") {
    // 펌웨어 재업로드용. 응답을 먼저 내보내고 넘어간다(넘어가면 포트가 사라진다).
    Serial.println("OK: DFU");
    Serial.flush();
    delay(50);
    enterDfu();
#endif

  } else if (cmd == "PING") {
    Serial.println("OK: PONG");

  } else {
    Serial.print("ERR: UNKNOWN COMMAND: ");
    Serial.println(cmd);
  }
}

// "M2 START" → 모터 인덱스 1 + "START". 프리픽스가 없으면 M1(호환).
void dispatch(String cmd) {
  int mi = 0;
  if (cmd.length() >= 3 && cmd[0] == 'M' && isDigit(cmd[1]) && cmd[2] == ' ') {
    mi = cmd[1] - '1';
    if (mi < 0 || mi >= NUM_MOTORS) {
      Serial.print("ERR: NO MOTOR ");
      Serial.println(cmd[1]);
      return;
    }
    cmd = cmd.substring(3);
    cmd.trim();
  }
  handleCommand(mi, cmd);
}

void setup() {
  // 둘 다 첫 analogWrite 보다 먼저 — 이후 호출부터 적용된다. 주파수는 전 핀 공통이다.
  analogWriteFrequency(PWM_HZ);
  analogWriteResolution(PWM_BITS);
  pwmConflict = !pwmPinsOk();

  // 부팅 상태: 모든 모터 정지(브레이크), enabled=true, 방향 CW, 목표 듀티 DEFAULT_DUTY.
  for (int i = 0; i < NUM_MOTORS; i++) {
    pinMode(motors[i].in1, OUTPUT);
    pinMode(motors[i].in2, OUTPUT);
    stopMotor(motors[i]);   // EN 핀은 analogWrite 가 PWM 핀으로 잡는다
  }

  // 서보는 여기서 attach 하지 않는다(gateAttached 주석 참조). SERVO 명령이 올 때 잡는다.

  lineBuf.reserve(32);
  Serial.begin(115200);   // USB CDC 는 baud 를 무시하지만 관례상 둔다
  delay(300);
  // 네이티브 USB 라 포트를 열어도 리셋되지 않는다. 이 배너는 '부팅 직후 호스트가
  // 이미 듣고 있던 경우'에만 보이고, 호스트는 배너가 없어도 진행한다(_wait_boot).
  Serial.println("READY: 2-Axis DC Conveyor Controller Initialized (BlackPill F411, HW-095/L298N)");
  if (pwmConflict) {
    Serial.println("ERR: PWM 타이머 겹침 또는 PWM 불가 핀 — 핀 배정을 확인할 것. START 는 거부된다");
  }
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
      lastLineMs = millis();   // 줄을 받았다 = 호스트가 살아 있다. 빈 줄·모르는 명령도 센다.
      lineBuf.trim();
      if (lineBuf.length() > 0) dispatch(lineBuf);
      lineBuf = "";
    } else if (c != '\r') {
      lineBuf += c;
    }
  }

  // 호스트 워치독(안전). 달리는 모터가 있는데 HOST_TIMEOUT_MS 넘게 줄이 없거나, USB CDC 의 DTR 이
  // 내려갔으면(호스트가 포트를 닫음) 모든 모터를 세운다. 서 있으면 running 이 거짓이라 한 번만 걸린다.
  // `!Serial` 대신 `Serial.dtr()` 를 쓴다: STM32duino core 3.0.0 의 USBSerial::operator bool 은 호출마다
  // delay(10) 을 하고 같은 dtrState 를 돌려주므로(libraries/USBDevice/src/USBSerial.cpp) 매 loop 에서
  // 부르면 루프가 100Hz 로 굳는다. DTR 이 내려가 있으면 CDC 쓰기는 막힘 없이 버려지므로 WARN 줄을
  // 받을 호스트가 없어도 여기서 멈추지 않는다. 정지가 먼저고 알림은 그다음이다.
  if (anyRunning() && (!Serial.dtr() || millis() - lastLineMs > HOST_TIMEOUT_MS)) {
    stopAll();
    Serial.println("WARN: HOST TIMEOUT");
  }

  rampTick();
}
