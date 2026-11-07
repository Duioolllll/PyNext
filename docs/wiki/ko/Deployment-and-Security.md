# 배포와 보안 (실험 버전)

[홈](Home.md) · [English](../en/Deployment-and-Security.md)

**PyNext v0.6.0a2를 보안 강화가 완료된 공개 프로덕션 서버로 취급하면 안 됩니다.** 저장소도 인터넷 직접 노출 및 신뢰할 수 없는 앱 코드 실행을 권장하지 않습니다.

## 신뢰할 수 있는 로컬 앱 실행

~~~bash
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic --host 127.0.0.1 --port 3000
~~~

`start`는 매니페스트, 추적되는 Python·공개 파일·island 소스 해시, 생성 에셋의 무결성을 검증합니다. 그러나 **외부 의존성, 환경 변수, 외부 데이터까지 전부 추적하지는 않습니다.** 변경 후에는 재빌드가 필요합니다. 빌드 과정 자체에서 개발자가 제공한 Python 모듈이 import/실행되므로 모르는 프로젝트를 관리자 권한으로 빌드하면 안 됩니다.

운영 모드 시작에도 앱 소스의 `public/` 및 `islands/` 디렉터리가 필요합니다. **Standalone 배포 산출물은 없습니다.**

## 외부 ASGI 서버

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

위 예제 진입점은 **개발 모드**입니다. 빌드된 SSG를 이용하려면 별도 진입점에서 `create_app(project_path, production=True)`로 앱을 만들어야 합니다. 외부 ASGI 서버의 타임아웃은 해당 서버가 관리합니다.

## 보안 경계 및 미구현 기능

- 요청 본문 크기는 1 MiB로 제한하며 잘못된 데이터는 검증 시점에 400을 반환합니다.
- 기본 CSP는 `script-src 'self'; object-src 'none'`입니다.
- Island props는 공개값이므로 비밀번호·토큰을 넣지 마세요.
- ISR은 공개 캐시이며 `Cookie`/`Authorization` 요청은 공유 캐시를 우회합니다.
- 커스텀 캐시 무효화 API에는 인증·권한 검사가 필수입니다.
- 동기 Python 작업은 타임아웃 시 강제 종료할 수 없습니다. DB 타임아웃·멱등성을 적용하세요.
- WebSocket, 프로덕션 관측성, 전체 연결 제한, 워커 공유 캐시, 프로세스 격리는 미구현입니다.
- 검증되지 않은 프록시 헤더를 신뢰하지 마세요. h2c를 TLS 종료 기능으로 오해해서도 안 됩니다.

자세한 설계 후보는 [프로덕션 로드맵](../../PRODUCTION_ROADMAP.md)에 있습니다. **로드맵은 완료된 기능 목록이 아닙니다.**
