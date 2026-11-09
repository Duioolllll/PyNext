# Docker 배포와 공급망 검사

개발 버전은 저장소 루트의 `Dockerfile`로 기본 예제를 빌드한다. Rust·Node·uv는
빌드 단계에만 있고 실행 단계에는 Python·프레임워크·앱과 프로덕션 산출물만 있다.
실행 이미지에는 pip/ensurepip를 두지 않는다. 의존성 변경은 새 이미지로 배포한다.

```bash
docker build --tag pynext-example:local .
python tests/smoke_docker.py --image pynext-example:local
docker compose -f deploy/compose.yaml up --build -d
```

`APP_DIR` 빌드 인자(기본 `examples/basic`)로 저장소 내 앱을 선택한다. 별도 앱의
추가 의존성은 이미지 빌드 정의와 잠금 파일에 추가해야 한다. 예제는 추가 DB·이미지
extras를 설치하지 않는다. 컨텍스트의 환경 파일·키 파일·Git·빌드 캐시는
`.dockerignore`로 제외한다. 앱 코드 실행이 포함된 빌드에는 신뢰한 소스만 사용한다.

프록시 CA가 필요한 빌드는 시스템 CA와 프록시 CA를 합친 공개 번들을 secret
`proxy_ca`로 전달한다. 각 네트워크 단계에서만 마운트되며 이미지에 복사하지 않는다.

```bash
docker build --secret id=proxy_ca,src=/path/to/combined-ca.pem --tag pynext-example:local .
```

Compose는 UID/GID 10001, 읽기 전용 루트, 64 MiB 임시 디렉터리, capability 제거,
권한 상승 금지, PID·메모리·CPU 상한과 40초 종료 유예를 설정한다. 기본 캐시는
프로세스 메모리다. SQLite 세션·디스크 캐시가 필요하면 해당 경로에만 별도 쓰기
볼륨을 연결하고 소유권을 설정한다. 자격 증명은 이미지나 Git에 넣지 않는다.

호스트의 `127.0.0.1:3000`으로만 노출한다. 외부 운영 연결에는
[ADR-0001](adr/0001-tls-termination.md)의 TLS 리버스 프록시가 필요하다.
프록시는 클라이언트 전달 헤더를 덮어쓰고 실제 프록시 주소만 신뢰하도록 설정한다.
`/readyz`는 시작 완료·드레인 상태와 `fail_closed` 공유 백엔드의 건강을 반영하고, `/healthz`는 프로세스 생존 검사다.
`SIGTERM`을 Python 프로세스가 직접 받아 드레인한다. 기본 이미지의 예제 이외에
배포할 앱·스토리지·프록시는 별도로 통합 검증해야 한다.

## 자동 검사

[Supply chain 워크플로](../.github/workflows/security.yml)는 PR·push·주간 예약·수동
실행에서 아래를 수행하고 보고서를 Actions artifact에 보관한다. 이미지를 레지스트리에
게시하거나 릴리스를 생성하지 않는다.

| 대상 | 검사·산출물 | 실패 기준 |
| --- | --- | --- |
| Python | 잠긴 전체 extras의 pip-audit, 설치 환경 CycloneDX JSON | 알려진 취약점 1건 이상 |
| JavaScript | npm lockfile audit, 빌드 의존성 포함 | low 이상 1건 이상 |
| Rust | Cargo.lock cargo-audit | 보안 권고 1건 이상 |
| 실행 이미지 | Syft SPDX JSON, Trivy OS·Python 패키지 검사 | 수정 가능한 High/Critical 1건 이상 |

Python SBOM은 검사 도구·개발 extras도 포함한 전체 환경이고, 이미지 SBOM은 실제
최소 실행 환경이다. 플랫폼 조건이 다른 의존성은 해당 플랫폼에서 추가 검사해야 한다.
이미지에서 수정 버전이 아직 없는 취약점과 Low/Medium은 현재 차단 기준 밖이다.
검사 통과는 외부 보안 감사나 취약점 부재를 보장하지 않는다. 보고서와 차단 기준을
함께 검토한다. 서명·배포 provenance·재현 빌드와 고정 digest 업데이트 정책은 후속 범위다.

```bash
uv sync --locked --all-extras
mkdir -p security-reports
uv run --locked --all-extras cyclonedx-py environment .venv/bin/python --output-format JSON --output-file security-reports/python.cdx.json
uv export --locked --all-extras --no-emit-project --output-file security-reports/python-requirements.txt
uv run --locked --all-extras pip-audit --no-deps --disable-pip -r security-reports/python-requirements.txt
npm audit --audit-level=low
cargo install cargo-audit --locked --version 0.22.2
cargo audit
```

이번 검사에서 Pillow 12.3.0의 알려진 취약점 39건과 PyO3 0.25.1의 RustSec 권고
2건을 확인해 Pillow 13.0.0과 PyO3 0.29.3으로 갱신했다. 실행에 불필요한 pip와
vendored 의존성은 최소 실행 이미지에서 제거했다. 검사는 실행 당시의 권고 DB에
한정되며, 원본 보고서는 CI에서 각 커밋에 다시 생성한다.

## 로컬 검증 기록

Linux amd64 이미지의 크기는 122.70 MiB이며, 이미지를 로컬에 준비한 상태에서 컨테이너 생성부터 readiness까지 1.361초였다. 이미지 pull 시간은 포함하지 않는다. UID 10001·읽기 전용 실행과 SIGTERM 종료 코드 0을 확인했다. 전체 수치와 검사 범위는 [로컬 근거 JSON](evidence/0.1.1-local.json)에 보존한다. 환경이 다른 곳의 크기·시작 시간 보장은 아니다.
