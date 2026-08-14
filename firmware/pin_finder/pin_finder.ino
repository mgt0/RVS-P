// 진단 전용 (BlackPill F411): 어느 GPIO 가 실제로 TB6600 의 PUL 에 연결됐는지 찾는다.
//
// 사용법
//   1) 업로드(BOOT0+리셋 → DFU) 후 시리얼 모니터(115200)를 연다
//   2) 모니터에 "TESTING <핀>" 이 뜰 때마다 모터 3개를 본다
//   3) 축이 움직인 구간의 핀 이름 = 그 모터의 PUL 핀. 셋 다 적어서 알려줄 것.
//
// 원리: 후보 핀을 전부 같은 레벨(대기 레벨)로 두고 한 번에 하나만 20 sps 로
// 펄스한다. 20 sps 는 탈조가 불가능한 저속이라, 안 움직이면 그 핀은 PUL 이 아니다.
//
// 2패스인 이유: 배선이 공통 GND 냐 공통 5V(애노드)냐에 따라 ENA 옵토를 끄는
// 레벨이 반대다. PASS 1 은 대기 LOW(공통 GND 에서 드라이버 활성), PASS 2 는
// 대기 HIGH(공통 애노드에서 활성). 모터가 움직인 패스가 실제 배선 방식도 알려준다:
//   PASS 1 에서 움직임 = 공통 GND 배선, PASS 2 에서 움직임 = 공통 애노드 배선.

// 실제로 배선된 9핀만 훑는다(추천 핀표 1~9번 = 세 드라이버의 PUL/DIR/ENA).
// 여기서 아무 반응이 없으면 배선·전원 문제이므로 핀을 더 넓혀도 의미가 없다.
// 배선을 바꿔 다른 핀을 쓴다면 아래 두 배열에 그 핀을 추가할 것.
const uint32_t CANDIDATES[] = {
  PA0, PA1, PA2,   // M1 PUL/DIR/ENA
  PA3, PA6, PA7,   // M2 PUL/DIR/ENA
  PB0, PB1, PA8,   // M3 PUL/DIR/ENA
};
const char *NAMES[] = {
  "PA0 (M1 PUL)", "PA1 (M1 DIR)", "PA2 (M1 ENA)",
  "PA3 (M2 PUL)", "PA6 (M2 DIR)", "PA7 (M2 ENA)",
  "PB0 (M3 PUL)", "PB1 (M3 DIR)", "PA8 (M3 ENA)",
};
const int N = sizeof(CANDIDATES) / sizeof(CANDIDATES[0]);

const int PULSES = 60;           // 60 스텝. 마이크로스텝 8 이면 눈에 보이는 정도로 돈다
const int HALF_PERIOD_MS = 15;   // 약 33 sps — 탈조가 불가능한 저속

void sweep(int restLevel) {
  for (int i = 0; i < N; i++) digitalWrite(CANDIDATES[i], restLevel);
  delay(500);

  for (int i = 0; i < N; i++) {
    uint32_t pin = CANDIDATES[i];
    Serial.print(">>> TESTING ");
    Serial.println(NAMES[i]);

    for (int p = 0; p < PULSES; p++) {
      digitalWrite(pin, !restLevel);
      delay(HALF_PERIOD_MS);
      digitalWrite(pin, restLevel);
      delay(HALF_PERIOD_MS);
    }

    Serial.print("    ");
    Serial.print(NAMES[i]);
    Serial.println(" 끝 (움직였나요?)");
    delay(1500);
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);

  for (int i = 0; i < N; i++) pinMode(CANDIDATES[i], OUTPUT);

  Serial.println("=== PIN FINDER (BlackPill F411) 시작 ===");
  Serial.println("모터 3개를 모두 보세요. 움직인 구간의 핀 = 그 모터의 PUL.");
  delay(2000);

  Serial.println("--- PASS 1: 대기 LOW (공통 GND 배선용) ---");
  sweep(LOW);
  Serial.println("--- PASS 2: 대기 HIGH (공통 5V/애노드 배선용) ---");
  sweep(HIGH);

  Serial.println("=== 끝. '어느 패스'에서 '어느 핀'에 '어느 모터'가 움직였는지 알려주세요 ===");
}

void loop() {}
