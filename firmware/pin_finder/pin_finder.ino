// 진단 전용: 어느 GPIO 가 실제로 TB6600 의 PUL 에 연결됐는지 찾는다.
//
// 사용법
//   1) 업로드 후 시리얼 모니터(115200)를 연다
//   2) 모니터에 "TESTING GPIO n" 이 뜰 때마다 모터를 본다
//   3) 축이 움직이는 구간의 GPIO 번호 = PUL 핀
//
// 원리: 후보 핀을 전부 LOW 로 두고(= 옵토커플러 미트리거 = 드라이버 활성),
// 한 번에 하나씩만 20 sps 로 펄스한다. 20 sps 는 초당 20스텝이라 탈조가 불가능한
// 저속이므로, 안 움직이면 그 핀은 PUL 이 아니라는 뜻이다.

const int CANDIDATES[] = {4, 5, 6, 7, 15, 16};
const int N = sizeof(CANDIDATES) / sizeof(CANDIDATES[0]);

const int PULSES = 100;          // 100 스텝 = 풀스텝 기준 반바퀴
const int HALF_PERIOD_MS = 25;   // 20 sps

void setup() {
  Serial.begin(115200);
  delay(1500);

  for (int i = 0; i < N; i++) {
    pinMode(CANDIDATES[i], OUTPUT);
    digitalWrite(CANDIDATES[i], LOW);
  }

  Serial.println("=== PIN FINDER 시작 ===");
  Serial.println("모터를 보세요. 움직이는 구간의 GPIO 가 PUL 입니다.");
  delay(2000);

  for (int i = 0; i < N; i++) {
    int pin = CANDIDATES[i];
    Serial.print(">>> TESTING GPIO ");
    Serial.println(pin);

    for (int p = 0; p < PULSES; p++) {
      digitalWrite(pin, HIGH);
      delay(HALF_PERIOD_MS);
      digitalWrite(pin, LOW);
      delay(HALF_PERIOD_MS);
    }

    Serial.print("    GPIO ");
    Serial.print(pin);
    Serial.println(" 끝 (움직였나요?)");
    delay(2000);
  }

  Serial.println("=== 끝. 움직인 GPIO 번호를 알려주세요 ===");
}

void loop() {}
