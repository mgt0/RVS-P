// 사용자가 "이건 돌았다"고 확인해준 원본 코드. 기준점(reference)으로만 쓴다.
// 원본의 오타 `.println(...)` 한 줄만 `Serial.println(...)` 으로 고쳤다.
#include <Arduino.h>

#define J3_ENA  4
#define J3_DIR  5
#define J3_PUL  6

#define GEAR_RATIO           39.0f
#define MICROSTEPS           4
#define STEPS_PER_REV        200

const float STEPS_PER_DEG = (STEPS_PER_REV * MICROSTEPS * GEAR_RATIO) / 360.0f;

float mmToHz(float mm_s) {
  return mm_s * STEPS_PER_DEG;
}

bool  isRunning      = false;
float targetSpeed    = 0.0f;
float currentSpeed   = 0.0f;
float acceleration   = 1200.0f;

unsigned long lastPulseTime = 0;
unsigned long lastAccelTime = 0;
bool pulseState = false;

void printStatus() {
  Serial.println("\n===========================================");
  Serial.println("  J3 모터 제어 시스템 (ENA 제어 포함)");
  Serial.println("===========================================");
  Serial.println("  <숫자>   : 목표 속도 입력 (예: 200, 1000 mm/s)");
  Serial.println("  OFF      : 모터 힘 제거");
  Serial.println("  ON       : 모터 힘 복구");
  Serial.println("  S        : 즉시 정지 (STOP)");
  Serial.println("  R        : 재시작 (RUN)");
  Serial.println("  D        : 방향 전환 (DIR)");
  Serial.println("===========================================\n");
}

void setup() {
  Serial.begin(115200);

  pinMode(J3_ENA, OUTPUT);
  pinMode(J3_DIR, OUTPUT);
  pinMode(J3_PUL, OUTPUT);

  digitalWrite(J3_ENA, LOW);
  digitalWrite(J3_DIR, HIGH);
  digitalWrite(J3_PUL, LOW);

  printStatus();
}

String inputBuffer = "";

void loop() {
  unsigned long nowMicros = micros();

  while (Serial.available()) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      inputBuffer.trim();
      if (inputBuffer.length() > 0) {

        if (inputBuffer.equalsIgnoreCase("OFF")) {
          isRunning = false;
          currentSpeed = 0.0f;
          digitalWrite(J3_ENA, HIGH);
          Serial.println(">> [OFF] 모터 힘 제거됨");
        }
        else if (inputBuffer.equalsIgnoreCase("ON")) {
          digitalWrite(J3_ENA, LOW);
          Serial.println(">> [ON] 모터 힘 복구됨");
        }
        else if (inputBuffer.equalsIgnoreCase("S")) {
          isRunning = false;
          currentSpeed = 0.0f;
          targetSpeed = 0.0f;
          digitalWrite(J3_PUL, LOW);
          Serial.println(">> [STOP] 비상 정지");
        }
        else if (inputBuffer.equalsIgnoreCase("R")) {
          digitalWrite(J3_ENA, LOW);
          isRunning = true;
          Serial.println(">> [RUN] 모터 구동 시작");
        }
        else if (inputBuffer.equalsIgnoreCase("D")) {
          bool dir = digitalRead(J3_DIR);
          digitalWrite(J3_DIR, !dir);
          Serial.printf(">> [DIR] 방향 전환: %s\n", !dir ? "HIGH" : "LOW");
        }
        else if (inputBuffer.startsWith("A ") || inputBuffer.startsWith("a ")) {
          float accel = inputBuffer.substring(2).toFloat();
          if (accel > 0) {
            acceleration = accel;
            Serial.printf(">> [ACCEL] 가속도 변경: %.0f\n", acceleration);
          }
        }
        else {
          float spd = inputBuffer.toFloat();
          if (spd > 0) {
            digitalWrite(J3_ENA, LOW);
            targetSpeed = spd;
            isRunning = true;
            Serial.printf(">> [TARGET] 목표 속도: %.0f mm/s\n", targetSpeed);
          } else {
            Serial.println("ERR: 올바른 명령어를 입력하세요.");
          }
        }
        inputBuffer = "";
      }
    } else {
      inputBuffer += c;
    }
  }

  if (nowMicros - lastAccelTime >= 2000) {
    float dt = (nowMicros - lastAccelTime) / 1000000.0f;
    lastAccelTime = nowMicros;

    if (isRunning && currentSpeed != targetSpeed) {
      if (currentSpeed < targetSpeed) {
        currentSpeed += acceleration * dt;
        if (currentSpeed > targetSpeed) currentSpeed = targetSpeed;
      } else if (currentSpeed > targetSpeed) {
        currentSpeed -= acceleration * dt;
        if (currentSpeed < targetSpeed) currentSpeed = targetSpeed;
      }
    }
  }

  if (isRunning && currentSpeed > 0.1f) {
    float currentHz = mmToHz(currentSpeed);
    unsigned long pulseIntervalUs = (unsigned long)(1000000.0f / (currentHz * 2.0f));

    if (pulseIntervalUs < 3) pulseIntervalUs = 3;

    if (nowMicros - lastPulseTime >= pulseIntervalUs) {
      lastPulseTime = nowMicros;
      pulseState = !pulseState;
      digitalWrite(J3_PUL, pulseState);
    }
  }
}
