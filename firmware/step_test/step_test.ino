// 최소 격리 테스트: Serial/START-STOP 로직 없이 부팅하자마자 저속으로 200스텝을 돈다.
// 이걸로도 안 돌면 소프트웨어 문제가 아니라 100% 배선/드라이버/DIP 문제로 확정된다.

const int PUL_PIN = 6;
const int DIR_PIN = 5;
const int ENA_PIN = 4;

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);
  digitalWrite(ENA_PIN, LOW);   // 활성화 (기존과 동일한 극성)

  delay(1000);                 // 손 뗄 시간

  for (int i = 0; i < 200; i++) {
    digitalWrite(PUL_PIN, HIGH);
    delay(5);                  // 아주 느리게: 눈으로 스텝 하나하나 보이는 속도
    digitalWrite(PUL_PIN, LOW);
    delay(5);
  }
}

void loop() {}
