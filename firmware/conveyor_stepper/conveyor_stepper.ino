// [레거시] ESP32-S3 (LOLIN S3) 판. 현재 리그는 STM32F411 BlackPill 을 쓴다 —
// firmware/conveyor_blackpill/ 참조. 프로토콜은 동일하며 이 파일은 참고용으로 남긴다.
//
// ESP32-S3 (LOLIN S3) + TB6600 + NEMA17(1.8도): 컨베이어 UART 제어
//
// 배선 (공통 GND 방식)
//   TB6600 PUL-, DIR-, ENA- 세 핀을 전부 한 선으로 묶어 공통 GND를 만들고,
//   그 공통 GND를 ESP32 GND 핀에도 반드시 연결한다(리턴 경로가 없으면 신호가 안 먹힘).
//   TB6600 PUL+ -> PUL_PIN, DIR+ -> DIR_PIN, ENA+ -> ENA_PIN
//
// PC(Flask)가 USB(UART, 115200bps)로 한 줄 명령을 보내면 한 줄로 응답한다.
//
//   START            구동 시작 (저속에서 출발해 목표까지 가속)   -> OK: STARTED
//   STOP             정지 (홀딩 토크는 유지)                     -> OK: STOPPED
//   SPEED <sps>      목표 속도를 steps/sec 로 지정               -> OK: TARGET_SPS=...
//   RPM <rpm>        목표 속도를 모터축 회전수로 지정             -> OK: TARGET_RPM=...
//   DIR CW|CCW       회전 방향 (구동 중이면 잠깐 멈추고 재출발)   -> OK: DIR=CW
//   ENABLE|DISABLE   드라이버 활성/해제(DISABLE 은 축이 헐거워짐) -> OK: ENABLED
//   STATUS           현재 상태를 JSON 한 줄로                     -> {"running":true,...}
//   PING             생존 확인                                    -> PONG
//
// 응답 규칙: 성공은 "OK: ", 실패는 "ERR: " 로 시작한다. STATUS 만 '{' 로 시작하는
// JSON 한 줄이다. 호스트(backend/core/conveyor.py)가 이 접두사로 분기한다.
//
// 속도 튜닝(모터가 안 돌고 소리만 나면 = 탈조):
//   START_SPS 를 더 낮추고, RAMP_SPS_PER_SEC 도 낮춰 천천히 올린다.
//   반대로 너무 느리면 실행 중에 "RPM 60" 을 보내 올린다(재업로드 불필요).
//   NEMA17 은 정지 상태에서 곧바로 고속 펄스를 넣으면 따라오지 못하고 진동만 한다.
//   그래서 느린 속도로 출발해 목표까지 가속(ramp)한다.

const int PUL_PIN = 6;
const int DIR_PIN = 5;
const int ENA_PIN = 4;

// ── 모터/드라이버 제원 ────────────────────────────────────────────────
// 1.8도 모터 = 360/1.8 = 200 full step/rev.
// MICROSTEP 은 TB6600 DIP 스위치(S1~S3) 설정과 반드시 일치시켜야 한다.
// 값이 틀리면 RPM 명령과 실제 회전수가 그 비율만큼 어긋난다.
const float MOTOR_STEPS_PER_REV = 200.0;
const float MICROSTEP = 8;
const float STEPS_PER_REV = MOTOR_STEPS_PER_REV * MICROSTEP;  // 1회전 펄스 수

// ── 속도 ──────────────────────────────────────────────────────────────
// 800sps 는 풀스텝 기준 240RPM 으로, 부하가 걸린 NEMA17 이 탈조하기 쉬운 속도다.
// 확실히 도는 저속에서 시작하고 "RPM"/"SPEED" 로 올려가며 한계를 찾는다.
// 감속기(약 39:1)가 물려 있어 출력축 속도는 모터 RPM 의 1/39 이다.
// 그래서 sps 를 작게 잡으면 출력축이 사실상 정지로 보인다:
//   50 sps  -> 모터 1.9RPM, 출력축 0.05RPM (한 바퀴 21분)  ← 이것 때문에 "안 돈다"고 오진
// 8000 sps -> 모터 300RPM, 출력축 7.7RPM
// 실증: 원본 코드가 17,333 sps 까지 정상 구동됐다.
// 펄스는 LEDC(하드웨어 PWM)가 만든다. digitalWrite 토글 루프는 ESP32 에서
// 100~200kHz 부근이 한계라 그 위를 지시하면 조용히 못 따라간다.
// LEDC 는 MHz 까지 정확히 내주고 CPU 도 안 쓴다.
// microstep 32 기준 10,000RPM = 1,066,667 sps 라 상한을 그 위로 잡는다.
const float MIN_SPS = 10.0;
const float MAX_SPS = 1200000.0;
const float START_SPS = 500.0;            // 출발 속도(steps/sec)
const float RAMP_SPS_PER_SEC = 100000.0;  // 가속도. 8000sps 까지 약 0.08초

// 램프 중 LEDC 주파수를 갱신하는 주기. 매 루프 갱신하면 호출 비용만 커진다.
const unsigned long RAMP_UPDATE_US = 2000;

float targetSps = 8000.0;  // 목표 속도. 실행 중 변경 가능

// TB6600 은 방향 신호가 바뀐 뒤 최소 5us 뒤에 펄스가 와야 한다.
// 벨트 부하가 걸린 상태에서 즉시 반전시키면 탈조하므로 넉넉히 멈춘 뒤 재출발한다.
const unsigned long DIR_SETTLE_MS = 150;

bool running = false;
bool enabled = true;
bool dirCw = true;
float currentSps = START_SPS;
unsigned long lastRampUs = 0;
String lineBuf;

// ── 헬퍼 ──────────────────────────────────────────────────────────────
void setPulseHz(float hz) {
  // 0 을 주면 LEDC 가 출력을 멈추고 핀은 LOW 로 남는다.
  ledcWriteTone(PUL_PIN, hz < 1.0 ? 0 : (uint32_t)hz);
}

void applyDir() {
  digitalWrite(DIR_PIN, dirCw ? LOW : HIGH);  // 벨트가 반대로 가면 이 두 값을 바꾼다
}

void applyEnable() {
  digitalWrite(ENA_PIN, enabled ? LOW : HIGH);  // LOW = 드라이버 활성화
}

void beginRun() {
  running = true;
  currentSps = START_SPS;  // 매번 저속에서 다시 출발
  lastRampUs = micros();
  if (!enabled) {
    enabled = true;
    applyEnable();
  }
  setPulseHz(currentSps);
}

void endRun() {
  running = false;
  setPulseHz(0);
}

float spsToRpm(float sps) {
  return sps * 60.0 / STEPS_PER_REV;
}
float rpmToSps(float rpm) {
  return rpm * STEPS_PER_REV / 60.0;
}

bool setTargetSps(float sps) {
  if (sps < MIN_SPS || sps > MAX_SPS) return false;
  targetSps = sps;
  return true;
}

void printStatus() {
  // JSON 한 줄. 키를 늘릴 때 호스트의 status() 파서도 같이 본다.
  Serial.print("{\"running\":");
  Serial.print(running ? "true" : "false");
  Serial.print(",\"enabled\":");
  Serial.print(enabled ? "true" : "false");
  Serial.print(",\"dir\":\"");
  Serial.print(dirCw ? "cw" : "ccw");
  Serial.print("\",\"sps\":");
  Serial.print(running ? currentSps : 0.0, 1);
  Serial.print(",\"target_sps\":");
  Serial.print(targetSps, 1);
  Serial.print(",\"rpm\":");
  Serial.print(spsToRpm(running ? currentSps : 0.0), 2);
  Serial.print(",\"target_rpm\":");
  Serial.print(spsToRpm(targetSps), 2);
  Serial.print(",\"microstep\":");
  Serial.print((int)MICROSTEP);
  Serial.print(",\"steps_per_rev\":");
  Serial.print((int)STEPS_PER_REV);
  Serial.println("}");
}

void handleCommand(const String& cmd) {
  if (cmd == "START") {
    beginRun();
    Serial.println("OK: STARTED");

  } else if (cmd == "STOP") {
    endRun();
    Serial.println("OK: STOPPED");

  } else if (cmd.startsWith("SPEED ")) {
    // 실행 중 속도 튜닝. 탈조하면 낮추고, 너무 느리면 올린다.
    float v = cmd.substring(6).toFloat();
    if (setTargetSps(v)) {
      Serial.print("OK: TARGET_SPS=");
      Serial.println(targetSps, 1);
    } else {
      Serial.print("ERR: SPEED 범위는 ");
      Serial.print(MIN_SPS, 0);
      Serial.print("~");
      Serial.println(MAX_SPS, 0);
    }

  } else if (cmd.startsWith("RPM ")) {
    // 사람이 감으로 잡기 쉬운 단위. 1.8도 + MICROSTEP 로 sps 로 환산한다.
    float rpm = cmd.substring(4).toFloat();
    if (setTargetSps(rpmToSps(rpm))) {
      Serial.print("OK: TARGET_RPM=");
      Serial.println(spsToRpm(targetSps), 2);
    } else {
      Serial.print("ERR: RPM 범위는 ");
      Serial.print(spsToRpm(MIN_SPS), 2);
      Serial.print("~");
      Serial.println(spsToRpm(MAX_SPS), 2);
    }

  } else if (cmd.startsWith("DIR ")) {
    String arg = cmd.substring(4);
    arg.trim();
    arg.toUpperCase();
    bool wantCw;
    if (arg == "CW" || arg == "1") wantCw = true;
    else if (arg == "CCW" || arg == "0") wantCw = false;
    else {
      Serial.println("ERR: DIR 인자는 CW 또는 CCW");
      return;
    }

    if (wantCw != dirCw) {
      bool wasRunning = running;
      if (wasRunning) endRun();  // 부하 걸린 상태에서 즉시 반전 = 탈조
      dirCw = wantCw;
      applyDir();
      if (wasRunning) {
        delay(DIR_SETTLE_MS);  // 벨트가 멎을 시간
        beginRun();            // 저속에서 다시 가속
      }
    }
    Serial.print("OK: DIR=");
    Serial.println(dirCw ? "CW" : "CCW");

  } else if (cmd == "ENABLE") {
    enabled = true;
    applyEnable();
    Serial.println("OK: ENABLED");

  } else if (cmd == "DISABLE") {
    // 드라이버를 놓으면 홀딩 토크가 사라져 축을 손으로 돌릴 수 있다.
    // 벨트를 수동으로 옮기거나 열을 식힐 때 쓴다.
    endRun();
    enabled = false;
    applyEnable();
    Serial.println("OK: DISABLED");

  } else if (cmd == "STATUS") {
    printStatus();

  } else if (cmd == "PING") {
    Serial.println("PONG");

  } else {
    Serial.print("ERR: UNKNOWN COMMAND: ");
    Serial.println(cmd);
  }
}

void setup() {
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  // PUL 은 LEDC 가 잡는다(pinMode/digitalWrite 로 건드리지 않는다).
  ledcAttach(PUL_PIN, 1000, 8);
  setPulseHz(0);
  applyDir();
  applyEnable();  // 활성화 (홀딩 토크 유지)

  lineBuf.reserve(32);
  Serial.begin(115200);
  delay(1000);
  // 호스트는 포트를 연 뒤 이 줄을 기다린다(포트를 열면 DTR 로 ESP32 가 리셋되고,
  // 부팅 전에 보낸 명령은 그대로 버려진다). 문구 앞부분을 바꾸면 호스트도 같이 고친다.
  Serial.println("READY: Conveyor Controller Initialized");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      lineBuf.trim();
      if (lineBuf.length() > 0) handleCommand(lineBuf);
      lineBuf = "";
    } else if (c != '\r') {
      lineBuf += c;
    }
  }

  if (!running) return;

  unsigned long now = micros();
  if (now - lastRampUs < RAMP_UPDATE_US) return;  // 2ms 마다만 갱신

  // 가속: 목표 속도까지 서서히 올린다.
  if (currentSps < targetSps) {
    currentSps += RAMP_SPS_PER_SEC * ((now - lastRampUs) / 1000000.0);
    if (currentSps > targetSps) currentSps = targetSps;
  } else if (currentSps > targetSps) {
    // 감속은 즉시. 이 경로가 없으면 SPEED/RPM 으로 낮춘 값이 반영되지 않아
    // 계속 이전 고속으로 돈다(느려지는 방향은 탈조 위험이 없으므로 램프 불필요).
    currentSps = targetSps;
  }
  lastRampUs = now;

  // 펄스 자체는 LEDC 가 계속 내보내고 있다. 여기서는 주파수만 바꿔주면 된다.
  setPulseHz(currentSps);
}
