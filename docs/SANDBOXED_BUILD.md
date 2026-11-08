# Sandboxed build (격리 빌드)

`pynext build`는 신뢰 경계(trust boundary)를 가진다. 이 문서는 그 경계가
무엇인지, 그리고 신뢰하지 않는 앱을 어떻게 분리된 환경에서 빌드하는지
설명한다. (로드맵 A7)

## 빌드가 실행하는 것

`pynext build`는 단순히 파일을 복사하지 않는다. 소스 코드를 직접
실행한다 (`python/pynext/build.py`의 `write_manifest()`):

- 앱의 Python 모듈을 임포트한다 (`app/`의 페이지·레이아웃·API,
  `middleware.py`, `resources.py`).
- SSG 페이지의 생성기(`generate_static_params()` 등)를 실행해
  정적 페이지를 렌더링한다.
- island가 있으면 esbuild로 JS/TS 번들을 만든다.

즉, 빌드 대상 앱의 코드는 빌드 머신에서 임의 코드를 실행할 수 있다.
**신뢰할 수 없는 앱은 권한 있는 환경에서 직접 빌드하지 않는다.**
`pynext dev`도 앱 코드를 실행하므로 같은 주의가 필요하다.
자세한 내용은 [배포·보안 위키](wiki/en/Deployment-and-Security.md)를 참고한다.

## 이 레시피가 보장하는 것 / 보장하지 않는 것

`unshare`(util-linux, Linux 전용)를 이용한 아래 레시피는 다음을 보장한다.

- **네트워크 차단**: 샌드박스 안에서 외부 네트워크에 닿지 않는다.
- **소스 읽기 전용**: 체크아웃 전체가 읽기 전용으로 마운트된다.
  빌드가 소스 파일을 바꾸는 것을 막는다.
- **쓰기 위치 제한**: 빌드 산출물은 스크래치 디렉터리의
  `.pynext/`에만 기록된다. 빌드 후 소스 해시가 그대로인지 확인할 수 있다.
- **비특권 실행**: 샌드박스 안에서는 루트로 보이지만, 밖에서는
  빌드를 실행한 일반 사용자 권한이다. 생성된 파일은 그 사용자의 소유가 된다.
- **재현 가능**: 같은 도구와 소스에서는 두 번의 빌드가 바이트 단위로
  동일한 `.pynext/` 산출물을 만든다 (예제 앱으로 검증됨).

보장하지 않는 것:

- **파일시스템 은닉이 아니다.** 호스트의 다른 파일은 샌드박스에서도
  읽을 수 있다. 파일까지 완전히 숨기려면 컨테이너나 VM을 쓴다.
- **산출물 자체의 신뢰성을 보장하지 않는다.** 샌드박스에서 나온
  `.pynext/`도 빌드 전에는 검사하지 않은 코드의 결과물이다.
  배포 전에 매니페스트와 산출물을 검토한다.
- **자원 제한이 없다.** 빌드가 CPU·메모리를 과다 사용하는 것을 막지
  않는다. 필요하면 `timeout`이나 cgroup 제한을 추가로 둔다.

## 전제 조건

- Linux, `unshare` (util-linux) 설치.
- 비특권 사용자 네임스페이스가 허용돼 있어야 한다. 확인 방법:

  ```bash
  unshare --user --map-root-user --mount --pid --fork --net true && echo OK
  ```

  실패하면 배포판의 사용자 네임스페이스 설정을 확인한다
  (예: `kernel.unprivileged_userns_clone`).
- 호스트에서 빌드 환경을 미리 준비한다 (신뢰된 체크아웃에서만 수행):

  ```bash
  cd PyNext
  uv sync --extra dev   # .venv + 네이티브 확장 빌드
  npm ci                # island용 esbuild (island가 없는 앱은 불필요)
  ```

  샌드박스 안에서는 `uv run`을 쓰지 않는다 (`uv`가 환경을
  동기화하려고 쓰기를 시도할 수 있다). 이미 만든 `.venv/bin/pynext`를
  직접 실행한다.

## 레시피

```bash
REPO=/path/to/PyNext            # PyNext 체크아웃 (읽기 전용으로 마운트)
APP="$REPO/examples/basic"      # 빌드할 앱 (체크아웃 안팎 모두 가능)
OUT="$(mktemp -d)"              # 스크래치 디렉터리 (산출물이 여기에 남는다)
mkdir -p "$OUT/.pynext" "$OUT/home" "$OUT/tmp"
mkdir -p "$APP/.pynext"         # 마운트 지점 (.pynext/는 gitignore된 빌드 산출물 경로)

unshare --user --map-root-user --mount --pid --fork --net --ipc bash -c '
  set -euo pipefail
  mount --bind "$REPO" "$REPO"
  mount -o remount,ro,bind "$REPO"
  mount --bind "$OUT/.pynext" "$APP/.pynext"
  export HOME="$OUT/home" TMPDIR="$OUT/tmp" PYTHONDONTWRITEBYTECODE=1
  cd "$REPO"
  "$REPO/.venv/bin/pynext" build --dir "$APP"
'

ls "$OUT/.pynext"   # manifest.json 과 static/ 이 여기에 있다
```

각 단계의 의미:

- `unshare --user --map-root-user`: 호출자를 샌드박스 안의 루트로 매핑한다.
- `--mount --pid --fork`: 별도 마운트·PID 네임스페이스를 만든다.
- `--net`: 네트워크 네임스페이스를 새로 만들어 외부와 단절한다.
- `--ipc`: IPC 네임스페이스를 분리한다.
- `mount --bind` 후 `remount,ro,bind`: 체크아웃 전체를 읽기 전용으로 만든다.
- `mount --bind "$OUT/.pynext" "$APP/.pynext"`: 빌드가 쓰는 유일한
  출력 경로를 스크래치 디렉터리로 연결한다. 이 마운트가 없으면
  읽기 전용 소스 때문에 빌드가 실패한다.
- `PYTHONDONTWRITEBYTECODE=1`: 임포트 시 `__pycache__` 쓰기를 막는다.
- `HOME`/`TMPDIR`을 스크래치로 지정: 빌드 도구가 홈 디렉터리에
  쓰는 것을 막는다.

## 검증 방법

빌드가 올바른 격리 상태에서 돌았는지 확인한다.

```bash
# 1. 빌드 전 앱 소스 해시를 기록한다 (.pynext/ 제외).
find "$APP" -type f -not -path "*/.pynext/*" -not -path "*__pycache__*" \
  | sort | xargs sha256sum > /tmp/sandbox-before.sha256

# 2. 위 레시피대로 빌드한다.

# 3. 산출물이 스크래치에 있고, 소스가 그대로인지 확인한다.
test -f "$OUT/.pynext/manifest.json" && echo "manifest OK"
find "$APP" -type f -not -path "*/.pynext/*" -not -path "*__pycache__*" \
  | sort | xargs sha256sum > /tmp/sandbox-after.sha256
diff /tmp/sandbox-before.sha256 /tmp/sandbox-after.sha256 && echo "source unchanged"
```

격리 자체를 확인하려면 샌드박스 안에서 다음이 실패하는지 본다.

```bash
# 네트워크 차단: 외부 연결이 실패해야 한다.
python3 -c "import socket; socket.create_connection(('8.8.8.8', 53), timeout=3)"
# 소스 읽기 전용: 쓰기가 실패해야 한다.
touch "$REPO/.sandbox-write-test"
```

검증 기록 (2026-11-08, `main` d8560f8, Linux VM):

- `examples/basic` 샌드박스 빌드 성공 (13개 라우트, 종료 코드 0).
- 네트워크 연결 실패, 소스 쓰기 실패를 확인했다.
- `manifest.json`과 `static/`이 스크래치 디렉터리에만 생겼다.
- 빌드 전후 앱 소스 sha256이 동일했다.
- 같은 조건으로 두 번 빌드한 `.pynext/` 산출물이 바이트 단위로 동일했다
  (`manifest.json` sha256 일치).

## 다른 환경

- macOS/Windows에서는 `unshare`를 쓸 수 없다. 같은 성질(네트워크 차단,
  읽기 전용 소스, 스크래치 출력)을 제공하는 컨테이너나 VM으로 빌드한다.
  이 문서의 Docker 변형은 이 환경에서 검증되지 않았으므로,
  도입 전에 위 검증 방법과 같은 절차로 직접 확인한다.
- CI에서는 샌드박스 빌드 잡을 별도 잡으로 두고, 네트워크를 끈
  러너에서 위 레시피를 실행한다. CI 잡 자체가 신뢰할 수 없는 앱
  코드를 실행한다는 점을 러너 권한 설계에 반영한다.
