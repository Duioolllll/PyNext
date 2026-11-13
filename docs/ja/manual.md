# PyNext マニュアル — 日本語

[English](../en/manual.md) · [한국어](../ko/manual.md) · [简体中文](../zh-CN/manual.md)

このマニュアルは、チェックアウトした **0.1.3** の実験版のコードを対象とします。PyNext は Python と Rust で構築する実験的なフルスタックフレームワークです。Next.js、React、React Server Components との互換性はありません。現時点ではインターネットに直接公開したり、信頼できないアプリを実行したりしないでください。[変更履歴](../../CHANGELOG.md)と[今後の計画](../PRODUCTION_ROADMAP.md)も参照してください。

> このマニュアルは英語マニュアルの翻訳です。内容が異なる場合は英語マニュアルが優先されます。機能一覧はコードから自動生成されています — [FEATURES](../FEATURES.md) を参照してください。

## 必要な環境と起動

CPython 3.13・3.14、Rust/Cargo、[uv](https://docs.astral.sh/uv/) が必要です。対応環境は CI マトリクスで検証します。OS は Ubuntu・macOS・Windows（最新）、Python は CPython 3.13・3.14、デプロイモードはスタンドアロン Axum と Uvicorn 上の ASGI です（インプロセス ASGI モードはすべての CI ジョブで pytest が検査します）。ブラウザスモークは 3 つの OS すべてで実行します。サンプルは island を使うため、esbuild 0.28.1 のインストールに Node.js 18 以降と npm も必要です。

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout main
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

http://127.0.0.1:3000/、/about、/guides/intro、/news/example、/hydrate、/strategies、/api/users にアクセスできます。Ctrl+C で終了します。

`pynext dev --dir examples/basic` はビルドなしでソースを提供し、`app/`・`islands/`・`public/`・`pynext.config.py` の変更時に同じポートでサーバーを再起動します。`pynext start` はビルド manifest を使い、追跡対象のソースが変わると再ビルドを要求します。Island を使わないアプリには Node と esbuild は不要です。上記はリポジトリからのインストールであり、PyPI 公開を意味しません。

### ビルドモード

通常の `pynext build` は island バンドルのコンパイルと静的ページの生成の両方を行います。`pynext build --only-islands` は island のコンパイルのみ、`pynext build --only-static` は以前のコンパイル結果を使った静的ページの生成のみを行います。部分ビルドでは `pynext start` は不足している段階を示して起動を拒否します。`pynext build --static-only` は SSG ページ・public アセット・island バンドルを `.pynext/export`（`--export-dir` で変更可）にプレーンなファイルとして書き出し、`python -m http.server` でサーバーなしに提供できます。動的 SSR・ISR・API ルートがあるアプリは拒否されます。詳細は英語マニュアルを参照してください。

開発監視はネイティブ通知とファイルハッシュを使い、必要ならポーリングに切り替えます。JS/TS island の変更は該当モジュールを再ビルドし、ページ全体を再読み込みせず交換します。以前のクリーンアップが実行され、その island の状態は初期化されることがあります。他のソース変更ではサーバーを再起動してページを再読み込みします。壊れた変更では前の正常な版を維持します。`--port 0` のポートは再起動後も維持します。

## プロジェクト構造とルーティング

~~~text
app/
  layout.py
  page.py
  blog/[slug]/page.py
  docs/[...path]/page.py
  api/users/route.py
public/
  styles/site.css
islands/
  counter.ts
~~~

`page.py` はデコレーター付きの `Page`、`route.py` は HTTP ハンドラーをエクスポートします。`layout.py` は配下のページを囲みます。優先順位は静的パス > `[slug]` > `[...path]` > `[[...path]]` です。単一パラメーターは文字列、catch-all は文字列のリストです。省略可能な catch-all `[[...path]]` は 0 個以上のセグメントにマッチするため、`/docs/[[...path]]` は空のリストで `/docs` も提供します。必須 catch-all と省略可能 catch-all を同じレベルで併用することはできず、ビルド時に競合として失敗します。並列・インターセプトルートは未実装です。

括弧付きのディレクトリはルートグループです。`app/(marketing)/page.py` は `/` を提供し、グループ内のレイアウトもそのまま適用されます。グループ名は英数字・`_`・`-` のみで、2 つのグループが同じ URL にマッピングされるとビルドが失敗します。

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` で HTML ツリーを作成し、文字列はエスケープされます。SSR は既定でレスポンス全体をバッファーして返しますが、ページは UI チャンクをストリーミングすることもできます（下記のストリーミング SSR 参照）。任意の raw HTML を挿入する API はありません。

## アプリの import と移行

ページ、レイアウト、API モジュールでは `from .helpers import title` のような相対 import を利用できます。アプリごとにモジュール名前空間を分離し、プロジェクトルートをプロセス全体の `sys.path` に自動追加しません。v0.5 のルート基準の絶対 import が失敗する場合は、相対 import に変更するか、プロジェクトを Python パッケージとしてインストールしてください。外部パッケージは実行環境にインストールします。

## API リクエストとエラー

~~~python
# app/api/echo/route.py
from pynext import BadRequest, JSONResponse, route

@route("POST")
async def echo(request):
    data = await request.json()
    if not isinstance(data, dict) or "name" not in data:
        raise BadRequest("name is required")
    return JSONResponse({"name": data["name"]}, status=201)
~~~

`request.query` の各値はリストです。たとえば `request.query.get("q", [""])[0]` で最初の値を取得します。元のバイト列やパーセントデコード後の不正な UTF-8、1,000 個を超えるクエリフィールド、不正な JSON、非有限 JSON 定数は `BadRequest` となり、ASGI と Axum は詳細を含まない HTTP 400 を返します。解析は対象を**読み取った時だけ**行います。その他の未処理例外は HTTP 500 になります。

レスポンスの既定 Content Security Policy は `script-src 'self'; object-src 'none'` です。ブラウザーが読み込めるスクリプトが制限されるため、クライアントコードの追加時に考慮してください。レスポンスヘッダーとメディアタイプの制御文字は拒否されます。

状態を変更する API リクエストには CSRF 保護が適用されます（仕様 v0.2 §11）。セッションミドルウェアを入れると、すべての状態変更 API ルートが自動的に保護され、トークンはサーバー側セッションに結び付けられます。セッションミドルウェアがない場合は既定で無効で、double-submit 方式を使います。double-submit では予測不能なトークンを `pynext_csrf` Cookie に格納し、クライアントは同じ値を `X-CSRF-Token` ヘッダー（または `csrf_token` フォームフィールド）で送り返します。Cookie の値は HMAC で署名されるため、署名のない Cookie はトークンが一致しても拒否されます（サブドメインによる Cookie 注入を防止）。`Origin` と `Sec-Fetch-Site` も検査し、クロスサイトやサブドメインのページからのリクエストは有効なトークンがあっても拒否されます。GET、HEAD、OPTIONS は対象外で、それ以外のメソッドでトークンがない、または一致しない場合は HTTP 403 を返します。

~~~python
from pynext import JSONResponse, csrf_protect, new_csrf_token, route, set_csrf_cookie

@route("GET")
async def get_token(request):
    token = new_csrf_token()
    return set_csrf_cookie(JSONResponse({"token": token}), token, config=request.security)

@route("POST")
@csrf_protect
async def update(request):
    return JSONResponse({"ok": True})
~~~

セッションミドルウェアがある場合、セキュリティ設定を別途渡さなくても CSRF が自動的に有効になります。セッションがなく、すべての状態変更 API ルートを保護するには、`create_app` に `security=SecurityConfig(csrf_enabled=True)` を渡してください。トークンが証明するのは、Cookie を読めたページからのリクエストであることだけです。XSS で Cookie を読めるページは、他の double-submit 方式と同様にこれを回避できます。Cookie は既定で `SameSite=Lax` であり、`HttpOnly` ではありません（JavaScript が読む必要があります）。CSRF Cookie は既定で `Secure` のため、平文 `http://` でのローカル開発では `csrf_cookie_secure=False` をアプリ設定と `set_csrf_cookie()` の呼び出しの両方に渡してください（`pynext dev` は自動的にそう動作します）。マルチワーカーで運用する場合は、すべてのワーカーで同じ署名鍵を共有します。`--workers N` で起動すると、スーパーバイザーが 32 バイトの鍵を 1 つ生成するか（`--csrf-signing-key` / `PYNEXT_CSRF_SIGNING_KEY` が指定されていればそれを使い）、すべてのワーカーと共有するため、クラッシュやローリング再起動後に起動したワーカーでも、あるワーカーが発行した Cookie を検証できます。鍵は 16 バイト以上の文字列、`hex:<hex>`、`base64:<base64>` のいずれかで指定でき、空・短すぎる・復号できない鍵はランダムな鍵に黙ってフォールバックせず、起動時に失敗します。ワーカーを使わない単一プロセスのサーバーはプロセスごとのフォールバック鍵をそのまま使い、すべての設定が同一プロセス内にあるため問題ありません。

クロスオリジンリクエストは既定で拒否されます（仕様 v0.2 §11）。オリジンを設定しない限り、`Access-Control-Allow-Origin` ヘッダーは送信されません。ポリシーは `SecurityConfig` で `create_app` に渡します。

~~~python
from pynext import SecurityConfig, create_app

app = create_app(".", security=SecurityConfig(
    cors_allowed_origins=("https://example.com",),
    cors_allowed_methods=("GET", "POST"),
    cors_allowed_headers=("Content-Type", "X-CSRF-Token"),
    cors_allow_credentials=False,
    cors_max_age=600,
))
~~~

許可されたオリジンからのプリフライト（`Origin` と `Access-Control-Request-Method` を含む `OPTIONS`）には 204 と `Allow` および `Access-Control-Allow-*` ヘッダーを返し、許可されていないオリジン・メソッド・ヘッダーのプリフライトは 403 になります。許可されたオリジンからの実際のクロスオリジンリクエストには `Access-Control-Allow-Origin`（設定時は `Access-Control-Allow-Credentials: true` も）を付け、それ以外のオリジンには CORS ヘッダーを付けないため、ブラウザーはレスポンスを公開しません。`Origin` を含むリクエストへのレスポンスには常に `Vary: Origin` も付きます。同一オリジンのリクエストは CORS でブロックされないため、オリジンを設定しなくても同一オリジンのページはカスタムヘッダーを付けてアプリ自身の API を呼べます。`cors_allowed_origins=("*",)` はすべてのオリジンを許可します。`cors_allow_credentials=True` と一緒に設定ファイルに書くと、ロード時に `ConfigError` で拒否されるため、資格情報を使う場合は明示的なオリジン一覧を書いてください。`SecurityConfig` を直接作った場合（設定の検証を迂回）はリクエストのオリジンをそのまま返さず `Access-Control-Allow-Origin: *` を返します。ブラウザーは資格情報付きリクエストでワイルドカードを無視するため、クロスオリジンのページがレスポンスを読むことはできません。

静的ファイル、SSG ページ、キャッシュされた ISR レスポンスはネイティブサーバーが直接提供しますが、`Origin` を含むリクエストには ASGI レスポンスと同じ CORS ポリシーが適用されます（条件付き・範囲・HEAD レスポンスを含みます）。CORS は Python ディスパッチを経るレスポンス（API ルートと動的レンダリングページ）だけに適用されるものではありません。

既定の CSP は `script-src 'self'; object-src 'none'` です。`SecurityConfig(csp_nonce_enabled=True)` を設定すると、動的レンダリングページごとにリクエスト単位の新しい nonce が発行されます。ヘッダーは `script-src 'self' 'nonce-<値>'; object-src 'none'` となり、同じ nonce が island ランタイムの `<script>` タグにも付きます。nonce はレスポンスごとに変わるため、nonce を含むページを自分でキャッシュしないでください。事前レンダリングされた SSG ページ、キャッシュされた ISR レスポンス、静的ファイルは nonce なしの静的ポリシーのままです。共有 HTML に埋め込まれた nonce は複数レスポンスで再利用され、nonce の意味がなくなるためです。Cookie または Authorization ヘッダー付きの ISR リクエストはリクエストごとに新たにレンダリングされるため、nonce が適用されます。

## セッション

サーバー側セッションは `SessionMiddleware` を導入すると `request.state["session"]` に入ります（`SQLiteSessionStore` はワーカーで共有する単一ファイル用、`RedisSessionStore` は Redis 用）。セッション Cookie には不透明なトークンのみが入り、`HttpOnly` で、既定では `Secure`、`SameSite=Lax` です。セッションデータは JSON シリアライズ可能でなければなりません。シリアライズできない値（任意のオブジェクトや集合など）を代入すると、保存時に失敗するのではなく、代入時にキーを明示した `TypeError` が発生します。保存データは最大 16 KiB です。セッション Cookie 名を複数回提示したリクエストは、セッションがないものとして扱います（fail-closed）。

ログイン時にセッション id をローテーションしてください — 任意ではなく必須です。資格情報を確認し、認証された識別情報を保存したら、ログイン応答を返す前に `session.rotate_on_login()` を呼びます：

~~~python
from pynext import JSONResponse, route

@route("POST")
def login(request):
    user = authenticate(request)  # 資格情報を検証します。失敗時は 401 を返す/発生させます
    session = request.state["session"]
    session["user_id"] = user.id
    session.rotate_on_login()  # 新しいセッション id + 新しい CSRF トークン。古い id は使えなくなります
    return JSONResponse({"ok": True})
~~~

ローテーションを省略するとセッションは固定化攻撃にさらされます。攻撃者がログイン前に固定したセッション id が、ログイン後も有効なままになります。権限が変わるとき（パスワード変更後など）も再度ローテーションしてください。ログアウト時には `session.invalidate()` を呼び、サーバー側セッションを削除して Cookie を消去します。

`RedisSessionStore` は Redis に到達できないとき障害ポリシー（`failure_policy` 参照）に従います。既定は `fail_closed` で、`SessionUnavailable` を発生させ、セッション状態を黙って失うのではなくリクエストに 503 で応答します。`failure_policy="fail_open"` を渡すと、代わりに新しく作った一時的なセッションで開始します。読み取りは続きますが何も永続化されず、障害中はログインを完了できません。発行された CSRF トークンが保存されないため、ログインリクエストは CSRF（403）で失敗します。障害中のローテーションでは、古いセッション id が有効期限まで使えるままになる場合があります。`fail_open` は、セッションの継続性より可用性が重要な場合にのみ使ってください。

`fail_open` ではログアウト時のセッション削除もベストエフォートです。ブラウザーの Cookie が消えても、サーバー側セッションとコピー済みのトークンは TTL 満了または復旧後の削除成功まで有効な場合があります。このログアウトはサーバー側の失効完了を保証しません。

## API レスポンスのストリーミング

`StreamingResponse` に文字列またはバイト列を返す非同期 iterable を渡します。ASGI と Axum はチャンクを順に送信し、完了・切断・エラー時に iterator を閉じます。各チャンクは最大 1 MiB です。`Content-Length` と `Transfer-Encoding` はサーバーが管理します。Axum の待機チャンクは 1 個で、次のチャンク生成と送信待機にはそれぞれ 30 秒の制限があります。ヘッダー送信後のエラーはステータスを変更せずストリームを中断します。HEAD はストリームを消費しません。React/RSC ストリーミングは未対応です。

## ストリーミング SSR

ページ関数は UI ノードの非同期 iterable を返せます。SSR はレイアウトと各チャンクを生成順に送信し、SSG はビルド時に結合して静的ファイルとして配信します。公開 ISR は最初の要求にストリーミングし、同時要求は完成した結果を待つか stale を受け取ります。完成した結果のみをキャッシュします。SSG ページは 8 MiB が上限で、SSG・ISR のストリームはさらに 30 秒が上限、ISR キャッシュの本文は 2 MiB が上限です。最初のクライアント切断後も生成が続く場合があります。レイアウトは children を一度だけ描画してください。[サンプル](../../examples/basic/app/stream/page.py)。

~~~python
from pynext import h, page

@page()
async def Page():
    yield h("h1", {}, "Streaming SSR")
    yield h("p", {}, "Each chunk is sent as it is produced.")
~~~

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
~~~

## WebSocket

`route.py` は HTTP ハンドラとあわせて `@websocket` ハンドラを 1 つエクスポートできます。ハンドラは `WebSocket` を受け取り、送受信の前に `await websocket.accept()` を呼ぶ必要があります。テキストとバイナリのメッセージをやり取りでき、相手が切断すると `receive()` で `WebSocketDisconnect` が発生するため、接続ごとのクリーンアップは `finally` ブロックに書きます。

開発版は ASGI・Axum で接続前に `Origin` を検査し、既定では同一オリジンのみを許可します。追加する HTTP(S) オリジンは `SecurityConfig(websocket_allowed_origins=("https://client.example",))` を `create_app(..., security=...)` に渡すか、`--ws-allowed-origin https://client.example` を繰り返し指定します。スキームと既定以外のポートを含めます。`null` は使えません。ワイルドカードパターンも使えます：`*` は DNS ラベル1つ（`https://*.example.com`）、`**` は先頭ラベルでのみ1つ以上の先頭ラベル（`https://**.example.com`）に一致し、`https://*.com` のようにドメイン全体に一致するパターンは拒否されます。`Origin` を送らない既存の非ブラウザークライアントには `websocket_allow_missing_origin=True` または `--ws-allow-missing-origin` が必要です。公開済み 0.1.0 にはこの検査はありません。`Origin` は偽装・省略できます。ハンドシェイクの前に、アップグレード要求にはグローバルミドルウェアチェーン（セッションミドルウェアを含む）とハンドラの `@require_auth`/`@require_permission` ポリシーが ASGI・Axum の両方で適用され、拒否されたアップグレードは 101 応答の前に拒否されます（ASGI・Axum ともに HTTP 403 で、拒否はログに残ります）。そのためハンドラは実行されません。身元の確立（例: ログインルートで `session["user_id"]` を設定すること）は引き続きアプリの責任であり、セッション以外の資格情報方式（トークンなど）はミドルウェアで検証してください。プロキシ利用時は実際のプロキシだけを `--trusted-proxy` で指定し、クライアント由来の転送ヘッダーをプロキシが上書きするよう設定してください。

ASGI で PyNext が信頼プロキシを識別するには、サーバーが `scope["client"]` に元の接続元を保持する必要があります。PyNext で転送ヘッダーを解釈する場合は、ASGI サーバー側での先行処理を無効にしてください（Uvicorn は `--no-proxy-headers`）。

WebSocket 認可ミドルウェアではセッションを作成・ローテーション・無効化しないでください。事前検査の応答は `Set-Cookie` とともに破棄されるため、新トークンがブラウザーに届かず、旧トークンはすでに削除されている場合があります。ログインやセッション変更は接続前の通常の HTTP エンドポイントで行ってください。

認可の事前検査を拒否すると、ASGI は受理前にコード 1008 の `websocket.close` を送り、ASGI サーバーは通常 HTTP 403 でハンドシェイクを拒否します。Axum は事前検査の 401/403 応答を HTTP 403 に、それ以外の応答やエラーを 503 に変換します。これは 429 を返せる組み込みハンドシェイクレート制限とは別の処理です。

~~~python
# app/ws/echo/route.py
from pynext import WebSocket, WebSocketDisconnect, websocket

@websocket
async def echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            kind, data = await websocket.receive()
            if kind == "text":
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)
    except WebSocketDisconnect:
        pass
~~~

[echo の例](../../examples/basic/app/ws/echo/route.py)を参照してください。ASGI と Axum はテキスト・バイナリに対応します。上限の既定値は 64 KiB で、`create_app(..., websocket_max_message=...)` または `--ws-max-message-bytes` で最大 16 MiB まで設定できます。受信超過時は 1009 で閉じます。`@websocket(protocols=("chat.v1", "chat.v2"))` でサブプロトコルを選択し、`websocket.subprotocol` で確認します。`WebSocketPubSub` は `async with broker.subscribe(topic) as sub`、`await sub.receive()`、`await broker.publish(topic, message)` により単一プロセス内で配信します。遅い購読者には `PubSubOverflow` が発生し、ワーカー間配信や永続化は行いません。Axum ではハンドラー前に握手が完了します。

## SSR、SSG、ISR

- `@page()` はリクエストごとにレンダリングします。UI ノードの非同期 iterable を返すと、バッファーせずにストリーミングします（ストリーミング SSR 参照）。
- `@page(render="ssg")` はビルド時にレンダリングします。動的 SSG ルートは辞書のリストを返す `generate_static_params()` が必要です。本番モードで未生成のパスは 404 になります。
- `@page(render="isr", revalidate=30, stale=120)` は公開ページをオンデマンドで生成し、fresh の間は再利用します。stale の間は古い結果を返しながら再生成を一つ実行します。時間単位は秒です。

~~~python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

SSG と ISR は `request` を受け取れず、公開コンテンツに使用します。ISR は通常クエリを無視します。`@page(render="isr", revalidate=30, query_keys=("lang",))` と `query` 引数を使用すると、許可したキーと文字列のリストだけが渡され、値ごとにキャッシュされます。パスの無効化は全クエリ変種に適用されます。Cookie または Authorization 付きの要求は共有キャッシュを迂回し `private, no-store` を返します。既定のキャッシュはプロセス内で、SQLite またはオプションの Redis（`pynext-framework[redis]` と `PYNEXT_REDIS_URL`）を共有できます。共有ストア障害時は障害ポリシーに従います。既定値（`fail_open`、`--cache-failure-policy fail_open`）では古いローカルキャッシュにフォールバックし、`fail_closed` では 503 を返します。`Set-Cookie` と private/no-store の応答はキャッシュしません。SSG の非同期ジェネレーターはビルド時に消費され静的ファイルとして配信されます。

SSG ビルドの上限は 10,000 ページで、開発モードでは SSG をリクエストごとにレンダリングします。ISR ページは `generate_static_params()` をエクスポートできず、同時生成はプロセスごとに最大 128 件です。

認可済み API ハンドラーでは `await request.revalidate("/news/example")`、アプリを保持するコードでは `await app.revalidate(path)` を使えます。クエリ文字列を含まないデコード済み ISR パスを渡します。ローカルまたは共有キャッシュのエントリを削除した場合、あるいは進行中の生成を無効化した場合は `True` です。共有バックエンドがある場合、ストアに到達できるときは共有エントリも削除されますが、`fail_open` モードでは共有ストアに到達できないときに `True` でも共有エントリが削除されたことは保証されません（共有削除はベストエフォートのため、他ワーカーは有効期限まで古いエントリを提供し続ける場合があります）— 適用されなかった無効化を成功として報告しないためには `fail_closed` を使ってください。既存リクエストは古い生成結果を受け取ることがありますが、その結果はキャッシュを再上書きしません。メモリバックエンドでは無効化はプロセス内のみで、他のワーカーには伝播しません。SQLite バックエンドでは共有エントリも削除され、正常な共有ストアを使う他ワーカーの次の参照に反映されます。公開するエンドポイントでは認証・認可が必要です。

## 公開ファイルと islands

公開ファイル、SSG ページ、island バンドルは SHA-256 に基づく強い ETag を持ちます。GET/HEAD は `If-Match` と `If-None-Match`、GET は単一バイト `Range` と強い ETag の `If-Range` に対応し、必要に応じて 206 または 416 を返します。複数または不正な範囲は無視し、全体を返します。日付ベースの検証と ISR の範囲リクエストは未対応です。

`public/styles/site.css` は `/styles/site.css` として公開されます。ビルド時にスナップショットと整合性検証を行いますが、本番起動時にも元ファイルが必要です。隠しファイル、シンボリックリンク、衝突するパスは拒否されます。開発モードで公開ファイルを変更するとサーバーは自動的に再起動します。

Island は Python の宣言と `islands/` 内の JavaScript または TypeScript モジュールを組み合わせます。

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter", props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

`islands/counter.ts` は `mount(element, props)` をエクスポートし、クリーンアップ関数を返せます。Props は公開される 16 KiB 以下の JSON オブジェクトです。秘密情報を入れないでください。ビルドには esbuild が必要です。ネストした island、npm パッケージ、外部 URL、CSS の import は未対応です。`/_pynext` は予約済みです。

サーバー DOM を再利用するには、サーバーの fallback と `hydrate=True` を指定し、モジュールから `render`、`hydrate`、`mount` をエクスポートします。[hydrated-counter.ts](../../examples/basic/islands/hydrated-counter.ts) を参照してください。ツリーが一致しない場合はその island を再 mount するため、入力状態やフォーカスが失われる場合があります。

`model_prop` を `pynext` からインポートし、`island("shared-model", props={"name": "Ada"}, hydrate=True, model=h("button", {"type": "button"}, "Hello ", model_prop("name")))` とすると、サーバーとブラウザーが同じモデルから初期 DOM を作ります。element・text・fragment、文字列 props、静的な文字列・真偽値・None 属性に対応し、16 KiB・512 ノード・深さ 32 が上限です。JS モジュールはイベント接続と不一致時の復旧に `hydrate`・`mount`・`render` をエクスポートします。動的属性・反復・クライアント状態の計算は対象外です。[サーバー例](../../examples/basic/app/shared-model/page.py)と[クライアント例](../../examples/basic/islands/shared-model.js)を参照してください。

有効化の既定値は `load` です。`visible` は画面内に入った時、`idle` はアイドルコールバックを要求して有効化します。アイドルコールバックには 2 秒の timeout を要求しますが、実行が遅れる場合があります。対応 API がない場合は idle またはタイマーに切り替わります。この 3 戦略は有効化前のイベントを再生しません。

`hydrate=True, strategy="interaction"` は、ユーザーが `<button type="button">` をクリックすると有効化します。[サンプルページ](../../examples/basic/app/interaction/page.py)と[モジュール](../../examples/basic/islands/interaction-counter.js)を参照してください。ローダーがその island にリスナーを登録した後の対応クリックは最大 16 件を保持し、hydration 成功後に順番に再生します。超過分は警告して破棄します。リンク、フォーム送信、入力、label、編集領域は捕捉・再生しません。リスナー登録前のイベントも捕捉できません。DOM の不一致、要素の削除、有効化の失敗時には待機イベントを破棄します。再生イベントは `isTrusted=false` の合成イベントで、ブラウザーのユーザー操作が必要な動作を保証しません。island 外のキャプチャリスナーは元と再生の両方を観測する場合があります。

## クライアント側ナビゲーション

すべてのページでクライアントランタイムを読み込みます。同一オリジンのリンクはメタデータと CSP が互換なら部分遷移し、それ以外は通常の遷移にフォールバックします。プリフェッチは同時 4 件、応答あたり 1 MiB までで、明示的に公開され有効な応答のみ最大 60 秒・20 件保存します。`Vary: Cookie` または `Authorization` を含む応答は保存しません。

## Python クライアント DSL（実験的）

`pynext.client_dsl.compile_client_dsl(source)` は、Python の小さく固定された部分集合を JavaScript にコンパイルします（仕様 §7.3）。対応するもの：リテラル（JavaScript の安全な整数範囲内の int、float、str、bool、None、list、tuple、dict）、算術演算（`+ - * / // % **`）、比較、`and`/`or`/`not`、`if`/`else`、`range()` またはリストに対する `for`、`while`、関数定義、`len`、`str`、`int`、`float`、`bool`、`abs`、`min`、`max`、`list`、`print`、`.append()`、インデックス、スライス、1 句のリスト内包表記です。それ以外はすべて `ClientDSLCompileError` になります。import、リフレクション、ファイル I/O、クラス、ラムダ、`try`、f-string、`open`・`os`・`Request`・`h` のようなサーバー専用の名前はコンパイル時に拒否されるため、サーバー側のコードがブラウザバンドルに混入しません。正確な一覧と既知の意味の違い（例：`str(1.0)` の `.0` が消える）は `pynext.client_dsl.SUPPORTED` と `KNOWN_DIFFERENCES` を参照してください。[例](../../examples/basic/client_dsl_example.py)もあります。DSL は単独のコンパイラで、island バンドルへの自動組み込みはしません。

非有限リテラルを拒否しても、実行時の結果が有限であることは保証されません。`float("Infinity")`、`float("nan")`、`1e308 * 1e308` のようなオーバーフローは Infinity・NaN を生成でき、実行時の有限性は検査しません。`KNOWN_DIFFERENCES` を参照してください。

## ASGI、タイムアウト、安全性

サンプルの ASGI アプリを Uvicorn で開発モード起動します。

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

ビルド済みアプリは `create_app(project_path, production=True)` で作成します。ASGI のタイムアウトは使用するサーバーが管理します。Axum アダプターは Python の応答を最大 30 秒待ち、キャンセルを要求して 504 を返します。既にスレッド上で動作中の同期関数は 504 の後も実行を続け、書き込みなどを行う場合があります。書き込みの冪等性と、DB・外部サービス固有のタイムアウトを設定してください。プロセス分離は `--workers`（下記）で提供され、単一プロセスでは利用できません。ISR のキャンセルにも同じ制限があります。

分散レート制限は `--rate-limit-backend redis` で実装されています（共有 Redis トークンバケット）。本番環境向けの安全性強化は引き続きロードマップ項目です。SQLite の無効化は共有ストアが正常な間、次の参照に反映されます。OpenTelemetry スパンは、任意パッケージをインストールし、トレーシングを有効化した場合に生成されます（可観測性を参照）。ASGI 適合性は `tests/test_asgi_conformance.py` で検証します（HTTP イベント順序、チャンク分割された本体、切断、失敗パスを含む lifespan）。計画と完了基準は[プロダクションロードマップ](../PRODUCTION_ROADMAP.md)を参照してください。

両アダプターのリクエスト本文は最大 1 MiB で、超過時は 413 です。Axum の既定値は本文受信 16 件、受信期限 10 秒（408）、Python 実行 64 件で、待機列は実行枠数と 64 の小さい方に制限され、満杯なら 503 です。実行数と本文設定は CLI で変更できます。HTTP/1.1 ヘッダー受信期限は 10 秒です。Axum の接続上限はワーカーごとに総数 1,024 件、IP ごとに 128 件が既定で、`--max-connections` と `--max-connections-per-ip` で変更できます。外部 ASGI サーバーの期限はそのサーバーで管理します。ルート以外の末尾 `/` は 308 でリダイレクトします。ビルド済み island は各 2 MiB、合計 32 MiB が上限です。

## 可観測性

構造化ログは既定で有効です。`pynext` ロガーは、1行につき `timestamp`、`level`、`message`、`request_id`、`trace_id`（`traceparent` ヘッダーを受け取った場合）、`route`、`status`、`latency_ms` を含む JSON オブジェクトを 1 つ出力します。各リクエストにはリクエスト ID が付与されます。有効な `X-Request-Id` ヘッダーがあればそれをそのまま使い、なければ新規に生成してリクエスト全体に伝播し、`x-request-id` 応答ヘッダーとして返します。ログメッセージにヘッダー、Cookie、本文、`Authorization` の値を決して含めないでください。JSON フォーマッタは認証情報らしい名前の追加フィールドを破棄し、アクセスログは生の URL ではなくルートパターン（例: `/blog/[slug]`）のみを記録します。

メトリクスはメモリ内で収集され、既定で有効です。`app.metrics.snapshot()` は、ルートパターン別のリクエスト数とステータスコード、ルート別の遅延 p50/p95/p99、Python 実行キューの長さと待機時間、Python 実行時間と Rust レンダリング時間、ISR キャッシュの hit/miss/stale と再生成の成功・失敗・開始、ワーカーの開始・タイムアウト・利用不可、応答サイズと island バンドルサイズを返します。`app.metrics.prometheus_text()` は同じカウンターを Prometheus テキスト形式で公開します。遅延サンプルはルートあたり 2048 件に制限されます。Rust が Python を経由せずに提供する新鮮な ISR ヒットはここでは数えません。Python ディスパッチに到達した ISR の結果のみが数えられます。

OpenTelemetry トレーシングは既定で無効であり、任意です。`opentelemetry-api`（および任意の SDK/エクスポーター）をインストールし、`--tracing` または `PYNEXT_TRACING=1` で有効化してください。有効な場合、`pynext.request` と `pynext.render` スパンが作成され、受信した W3C `traceparent`/`tracestate` コンテキストが伝播します。パッケージがインストールされていない場合、トレーシングは no-op で、何も壊れません。

ビルドテレメトリは `build_failed` イベントひとつのみをローカルに記録します。`pynext build` が失敗すると、ビルド段階（`discover`、`render`、`write` のいずれか）とビルド所要時間が `.pynext/telemetry.jsonl` に追加されます（直近 100 件まで保持）。イベントにプロジェクト名・パス・エラーメッセージ・ユーザーコードは含まれず、外部へ送信されるものは何もありません。ローカル記録を無効化するには `PYNEXT_TELEMETRY_DISABLED=1` を設定してください。

CLI フラグ `--structured-logs` / `--no-structured-logs`、`--metrics` / `--no-metrics`、`--tracing` / `--no-tracing`、環境変数 `PYNEXT_STRUCTURED_LOGS`、`PYNEXT_METRICS`、`PYNEXT_TRACING`、`PYNEXT_LOG_LEVEL`、またはコードで `create_app(..., observability=ObservabilityConfig(...))` を使って設定します。

CLI フラグ `--structured-logs` / `--no-structured-logs`、`--metrics` / `--no-metrics`、`--tracing` / `--no-tracing`、環境変数 `PYNEXT_STRUCTURED_LOGS`、`PYNEXT_METRICS`、`PYNEXT_TRACING`、`PYNEXT_LOG_LEVEL`、またはコード内で `create_app(..., observability=ObservabilityConfig(...))` により設定します。

既定の可観測性設定（構造化ログ + メトリクス有効、トレーシング無効）の測定オーバーヘッドは、ビルド済みサンプルで `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production` を各 2 回実行して測定しました。他の作業が同じ VM で並行してビルド・テスト中だったため、絶対遅延は膨らみ、実行間のばらつきが大きくなっています。有効: 25.4 / 14.2 req/s、p50 309 / 586 ms、プロセス CPU 4.79 / 5.02 秒。無効（`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`）: 18.7 / 18.5 req/s、p50 437 / 478 ms、CPU 5.01 / 4.69 秒。平均は有効 19.8 req/s・p50 447 ms に対して無効 18.6 req/s・p50 457 ms で、実行間のばらつきを超えるオーバーヘッドはありません（有効時の実行では stderr に JSON ログ約 1000 行も出力しています）。より厳密な値は静かなマシンで再測定してください。

## レート制限

レート制限は既定で有効で、プロセス単位のトークンバケットです。`pynext start` はクライアント IP あたり 60 秒で 300 リクエストを許可し、`--rate-limit 0` で無効にすると起動時に警告が出ます。`pynext start --rate-limit 100 --rate-limit-window 60` はクライアント IP あたり 60 秒で 100 リクエストを許可します（バーストが上限です）。制限を超えたリクエストは秒単位の `Retry-After` ヘッダー付きで 429 を返します。WebSocket のハンドシェイクも同じ方法でレート制限されます。ハンドシェイクごとに同じ IP 単位のバケットからトークンを 1 つ消費し、制限を超えたハンドシェイクは `Retry-After` 付きで 429 を返します。キーはクライアント IP で、`X-Forwarded-For` は `--trusted-proxy`（IP または CIDR、繰り返し指定可）で指定したピアからのものだけを信頼します。信頼できないプロキシのヘッダーは偽装可能なため無視されます。コードからは `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))` のように使います。バケットはプロセス単位で再起動すると消えます。`--rate-limit-backend redis` を使うとバケットは共有され、Redis に到達できない場合は障害ポリシーに従います。既定値（`fail_closed`、`--rate-limit-failure-policy fail_closed`）では要求に 503 を返し、`fail_open` では要求を許可します。設定した制限は両アダプターの静的ファイル、SSG、新鮮な ISR ページにも適用されます。

開発版は両アダプターで `Expect: 100-continue` を含む HTTP 要求を本文受信前のヘッダー段階で制限します。許可時にトークンを 1 つ消費し、後でアップロードを中断した場合やサイズ・速度・実行枠の制限で失敗した場合も返却しません。Axum の HTTP・WebSocket 制限判定は合計 64 件、期限 2 秒で、満杯・時間切れ・予期しないエラーは 503 と `Retry-After: 1` を返します。この枠は `fail_open` にも適用され、同ポリシーは期限内に返ったバックエンド障害のみを扱います。未受信の本文がある HTTP/1.1 の拒否接続は閉じ、HTTP/2 の他のストリームは維持します。ヘルス検査は制限を迂回し、例外パスは厳密なデコード後に判定し、エンコードされた疑問符をクエリー区切りとして扱いません。公開済み 0.1.0 にはこの変更はありません。

転送チェーンは右から信頼済みプロキシをたどり、最も近い非信頼アドレスを使います。中間プロキシをすべて正確に登録してください。バケットはプロセスあたり最大 10,000 個で、すべて使用中なら新しいキーは最も古いバケットを追い出して入ります(既存のキーに 429 は返しません)。新規キーの作成はウィンドウあたり最大 10,000 個のグローバルスライディングウィンドウで制限します。バケットのスコアは Redis `TIME` から取得した整数マイクロ秒です。2 つのスクリプトが同じマイクロ秒を読むことがあり、その場合、同点のバケットは等しく古いため、バケットキーで決定的に追い出すバケットを選びます。追い出された所有者は拒否されず、次のリクエストで新しいバケットを受け取ります。スコアは最大 1 年のウィンドウをはるかに超えても正確な整数のままです（現在のエポックでの最下位の単位は 0.25 µs）。そのため、数分から数時間の現実的なウィンドウは倍精度の限界の影響を受けません。IPv6 アドレスは /64 プレフィックスごとにバケットを共有します。このグルーピングはトレードオフです。/48 全体を持つ攻撃者は異なる /64 を 65,536 個作ってテーブルと新規キーウィンドウを満たすことができ、その間は本当に新しいキーがウィンドウが進むまで拒否されます(追い出された既存のキーに 429 は返しません)。`--rate-limit-ipv6-prefix-len 48` は /48 全体を 1 つのバケットにまとめ、この攻撃を防ぎますが、プレフィックスを共有するすべてのホストが 1 つのバケットを共有するため、混雑した /48 では無実のユーザーも一緒に制限されます。既定値は 64 のままです。`--rate-limit-exempt-path`(例: `/healthz`)で指定したパスは速度制限をスキップします。

複数ワーカー: `pynext start --workers 4` は一つのリスナーを共有するスーパーバイザーを起動します。`--port 0` の場合、割り当てたポートをワーカー再起動後も維持します。各ワーカーの実行環境とメモリキャッシュ、既定のレート制限は独立しています。SQLite・Redis ISR と Redis レート制限は共有できます。ワーカーは障害時に再起動し、処理中に中断した要求は再試行が必要です。共有リスナーを使うワーカーの起動と再起動は Linux・macOS・Windows の CI で検査し、シグナルを使うスーパーバイザーのテストは Linux・macOS で実行します。

## テストとトラブルシューティング

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

ルーターと HTML シリアライザには、個別の事例に加えて property-based テストもあります。Python 側は hypothesis（`tests/test_property_router_render.py`、固定シード・40 例で CI を高速に維持）、Rust 側は proptest（`crates/pynext-router` と `crates/pynext-native` の単体テスト、各 64 ケース）で、ルート照合の往復、エスケープ漏れなし、void タグ処理、任意のツリー描画を検査します。`embed` はブロック対象タグのため描画できず、対象外です。

ASGI 適合性は `tests/test_asgi_conformance.py` で検証します。HTTP イベント順序、複数チャンクの本体と 1 MiB のサイズ境界、読み取り中・低速ハンドラ実行中の切断、失敗時の `lifespan.startup.failed` / `lifespan.shutdown.failed` を含む lifespan の開始・終了、ストリーミング応答のフレーム順序を扱います。lifespan 失敗時に `.failed` メッセージを送るのは、このスイートとあわせて修正した動作です。

サンプルをビルドした後、Chromium で検証します。

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

`uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4` で小規模な閉ループ負荷テストを実行できます。全試行と成功した 2xx リクエストの秒間処理数、エラー・ステータス、全試行と成功分の p50/p95/p99、応答サイズ、CPU、最大 RSS を分けて記録します。既定モードの CPU とプロセス生存期間中の最大 RSS にはサーバーと負荷生成側の両方が含まれます。別のローカルサーバーには `--base-url http://127.0.0.1:3000` を指定し、Linux では `--server-pid <PID>` でそのプロセスとスレッドの CPU を個別に測定できます。子ワーカーは含みません。CI は本番モード（`--production`、ビルド済みサンプルを使用）で測定した小規模負荷の結果を、OS × デプロイモード（Axum/Uvicorn）のマトリクスごとに保存しますが、性能・メモリ回帰の自動しきい値はまだありません。`--compare` は axum・uvicorn・starlette の loopback HTTP グループを同一条件で実行します。2026-11-08 に記録した比較（`/api/users`、1000 リクエスト、同時実行数 10）は `benchmarks/results-2026-11-08.json` にあります。モードと注意点は `docs/BENCHMARKING.md` を参照してください。

- ソースの変更を指摘されたら `pynext build` を再実行します。
- esbuild が見つからない場合はリポジトリのルートで `npm ci` を実行するか、`PYNEXT_ESBUILD` に実行ファイルを指定します。
- Windows では npm の esbuild を Node 経由で実行します。`PYNEXT_ESBUILD` を直接設定する場合は、`.cmd`・`.bat` ではなくネイティブの `esbuild.exe` を指定してください。
- 動的 SSG パスが 404 の場合、`generate_static_params()` に追加して再ビルドします。
- Hydration が一致しない場合は、サーバー fallback とクライアント `render` の結果を決定的に一致させてください。
- ワーカーごとに ISR の結果が異なる場合は、キャッシュバックエンドを確認してください。メモリバックエンドはプロセス単位であり、SQLite バックエンドは共有ストアの障害時にプロセス内コピーへフォールバックします。

0.1.2 の ASGI HTTP/WebSocket は、未完了の判定を最大64件、各判定を2秒に制限します。課金前にパスを検証し、実行中の判定は完了まで枠を保持します。待機中に取消・期限切れとなった処理は limiter を呼びません。リクエスト全体の期限は ASGI ホストで別途設定します。[判定の契約](../ASGI_ADMISSION.md)を参照してください。
