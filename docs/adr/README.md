# ADR 목록

이 디렉터리는 PyNext의 아키텍처 결정 기록(Architecture Decision Records)을 둔다.

## 관례

- 파일명: `NNNN-짧은-제목.md` (번호는 0001부터 순서대로).
- 각 ADR은 상태(Status)를 가진다: `제안(Proposed)` → `채택(Accepted)` → `폐기(Deprecated)` / `대체됨(Superseded)`.
- 결정이 바뀌면 기존 ADR을 고치지 않고, 새 ADR을 만들어 이전 ADR을 대체한다.
  (이전 ADR의 상태를 `Superseded by ADR-XXXX`로 바꾼다.)
- ADR은 "무엇을 결정했는가"와 "왜 그렇게 결정했는가"를 기록한다.
  구현 방법은 해당 기능의 문서에 둔다.

## 목록

| 번호 | 제목 | 상태 |
| --- | --- | --- |
| [0001](0001-tls-termination.md) | TLS 종료는 리버스 프록시에서 수행한다 | Accepted |
