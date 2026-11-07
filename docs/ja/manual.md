# PyNext マニュアル — 日本語

[English](../en/manual.md) · [한국어](../ko/manual.md) · [简体中文](../zh-CN/manual.md)

このマニュアルは、チェックアウトした **v0.7.0a1** 未リリースの実験候補のコードを対象とします。PyNext は Python と Rust で構築する実験的なフルスタックフレームワークです。Next.js、React、React Server Components との互換性はありません。現時点ではインターネットに直接公開したり、信頼できないアプリを実行したりしないでください。[変更履歴](../../CHANGELOG.md)と[今後の計画](../PRODUCTION_ROADMAP.md)も参照してください。

## 必要な環境と起動

CPython 3.13、Rust/Cargo、[uv](https://docs.astral.sh/uv/) が必要です。対応環境は CI マトリクスで検証します。OS は Ubuntu・macOS・Windows（最新）、Python は CPython 3.13 のみ（3.14 は未検証のためマトリクスに含みません）、デプロイモードはスタンドアロン Axum と Uvicorn 上の ASGI です（インプロセス ASGI モードはすべての CI ジョブで pytest が検査します）。ブラウザスモークは 3 つの OS すべてで実行します。サンプルは island を使うため、esbuild 0.28.1 のインストールに Node.js 18 以降と npm も必要です。

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

状態を変更する API リクエストには CSRF 保護を選択的に適用できます（仕様 v0.2 §11）。認証システムはまだないため double-submit 方式を使います。アプリが予測不能なトークンを発行して `pynext_csrf` Cookie に格納し、クライアントは同じ値を `X-CSRF-Token` ヘッダー（または `csrf_token` フォームフィールド）で送り返します。GET、HEAD、OPTIONS は対象外で、それ以外のメソッドでトークンがない、または一致しない場合は HTTP 403 を返します。

~~~python
from pynext import JSONResponse, csrf_protect, new_csrf_token, route, set_csrf_cookie

@route("GET")
async def get_token():
    token = new_csrf_token()
    return set_csrf_cookie(JSONResponse({"token": token}), token)

@route("POST")
@csrf_protect
async def update(request):
    return JSONResponse({"ok": True})
~~~

ハンドラーを1つずつ指定する代わりに、`create_app` に `security=SecurityConfig(csrf_enabled=True)` を渡すと、すべての状態変更 API ルートを保護できます。セッションはまだないため、トークンはユーザーセッションに紐付きません。トークンが証明するのは、Cookie を読めたページからのリクエストであることだけです。XSS で Cookie を読めるページは、他の double-submit 方式と同様にこれを回避できます。Cookie は既定で `SameSite=Lax` であり、`HttpOnly` ではありません（JavaScript が読む必要があります）。本番環境では `csrf_cookie_secure=True` を設定し、HTTPS でのみ送信されるようにしてください。

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

許可されたオリジンからのプリフライト（`Origin` と `Access-Control-Request-Method` を含む `OPTIONS`）には 204 と `Allow` および `Access-Control-Allow-*` ヘッダーを返し、許可されていないオリジン・メソッド・ヘッダーのプリフライトは 403 になります。許可されたオリジンからの実際のクロスオリジンリクエストには `Access-Control-Allow-Origin`（設定時は `Access-Control-Allow-Credentials: true` も）を付け、それ以外のオリジンには CORS ヘッダーを付けないため、ブラウザーはレスポンスを公開しません。`Origin` を含むリクエストへのレスポンスには常に `Vary: Origin` も付きます。同一オリジンのリクエストは CORS でブロックされないため、オリジンを設定しなくても同一オリジンのページはカスタムヘッダーを付けてアプリ自身の API を呼べます。`cors_allowed_origins=("*",)` はすべてのオリジンを許可し、`cors_allow_credentials=True` の場合は `*` の代わりに正確なオリジンをそのまま返します。

静的ファイル、SSG ページ、キャッシュされた ISR レスポンスはネイティブサーバーが直接提供するため CORS ヘッダーは付きません。CORS は Python ディスパッチを経るレスポンス（API ルートと動的レンダリングページ）に適用されます。

既定の CSP は `script-src 'self'; object-src 'none'` です。`SecurityConfig(csp_nonce_enabled=True)` を設定すると、動的レンダリングページごとにリクエスト単位の新しい nonce が発行されます。ヘッダーは `script-src 'self' 'nonce-<値>'; object-src 'none'` となり、同じ nonce が island ランタイムの `<script>` タグにも付きます。nonce はレスポンスごとに変わるため、nonce を含むページを自分でキャッシュしないでください。事前レンダリングされた SSG ページ、キャッシュされた ISR レスポンス、静的ファイルは nonce なしの静的ポリシーのままです。共有 HTML に埋め込まれた nonce は複数レスポンスで再利用され、nonce の意味がなくなるためです。Cookie または Authorization ヘッダー付きの ISR リクエストはリクエストごとに新たにレンダリングされるため、nonce が適用されます。

## API レスポンスのストリーミング

`StreamingResponse` に文字列またはバイト列を返す非同期 iterable を渡します。ASGI と Axum はチャンクを順に送信し、完了・切断・エラー時に iterator を閉じます。各チャンクは最大 1 MiB です。`Content-Length` と `Transfer-Encoding` はサーバーが管理します。Axum の待機チャンクは 1 個で、次のチャンク生成と送信待機にはそれぞれ 30 秒の制限があります。ヘッダー送信後のエラーはステータスを変更せずストリームを中断します。HEAD はストリームを消費しません。React/RSC ストリーミングは未対応です。

## ストリーミング SSR

ページ関数は単一のツリーの代わりに UI ノードの非同期 iterable を返すことができます。レイアウトのシェルを先に送り、各チャンクを Rust が直列化して生成される順に送信し、最後にレイアウトの閉じ部分を送ります。既定は引き続き全体をバッファーする方式です。ストリーミングは SSR 専用で、SSG・ISR ページは完全なボディが必要なため、ストリーミングを指定すると失敗します。レイアウトは children をちょうど一度だけ描画しなければなりません。切断時はジェネレーターを閉じ、ヘッダー送信後のエラーはストリームを中断します。[サンプルページ](../../examples/basic/app/stream/page.py)は `/stream` で確認できます。

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

[echo の例](../../examples/basic/app/ws/echo/route.py)を参照してください。WebSocket ハンドラのないルートは close コード 1008 でハンドシェイクを拒否し、accept せずに返ったハンドラはコード 1000 で閉じられます。メッセージは 1 件あたり 64 KiB までで、それより大きいものは close コード 1009 で拒否されます。close コードは 1005・1006・1015 を除く 1000〜4999 で、reason は UTF-8 で 123 バイトまでです。サブプロトコル交渉、ブロードキャスト・pub/sub、接続間の共有状態はありません。各接続は自分のメッセージだけを受け取ります。WebSocket 専用の `route.py` は通常の HTTP リクエストに 405 で答えます。ASGI アダプタと Axum スタンドアロンサーバーの両方が WebSocket に対応しており、Axum ではハンドラ実行前にサーバーがハンドシェイクを完了するため、`accept()` は接続を accepted とするだけです。

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

SSG と ISR のページ関数は `request` を受け取れません。公開コンテンツにのみ使用し、環境変数や外部データが共有可能か確認してください。ISR は匿名リクエストのクエリ文字列をキャッシュキーに含めません。Cookie または Authorization があるリクエストは共有 ISR キャッシュを迂回し、`private, no-store` で返します。既定のキャッシュは上限付きの**プロセス内メモリ**であり、ワーカー間で共有・無効化されません。`--cache-backend sqlite` で起動するか `create_app(..., cache_backend="sqlite", cache_path=...)` で作成すると、すべてのワーカーが 1 つの SQLite ファイル（WAL）を共有します。stale の再生成は有効期限と所有者トークンを持つ lease によりキーごとに 1 ワーカーだけが行い、保存時にバージョンと有効な lease 所有権を原子的に検証します。共有ストアに障害があればワーカーごとのローカルキャッシュに切り替えます。各ワーカーは共有ファイルの前にプロセス内キャッシュを置くため、他ワーカーの無効化は共有エントリには即座に反映されますが、ローカルコピーは残りの TTL だけ残ることがあります。`Set-Cookie` や `Cache-Control: private/no-store` の応答は共有キャッシュに保存しません。Redis アダプターは将来の課題であり未実装です。SSG の内容を更新するには再ビルドが必要です。

SSG ビルドの上限は 10,000 ページで、開発モードでは SSG をリクエストごとにレンダリングします。ISR ページは `generate_static_params()` をエクスポートできず、同時生成はプロセスごとに最大 128 件です。

認可済み API ハンドラーでは `await request.revalidate("/news/example")`、アプリを保持するコードでは `await app.revalidate(path)` を使えます。クエリ文字列を含まないデコード済み ISR パスを渡します。キャッシュまたは進行中の生成を無効化した場合は `True` です。既存リクエストは古い生成結果を受け取ることがありますが、その結果はキャッシュを再上書きしません。メモリバックエンドでは無効化はプロセス内のみで、他のワーカーには伝播しません。SQLite バックエンドでは共有エントリも削除されますが、他ワーカーのプロセス内コピーは残りの TTL だけ残ることがあります。公開するエンドポイントでは認証・認可が必要です。

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

有効化の既定値は `load` です。`visible` は画面内に入った時、`idle` はアイドルコールバックを要求して有効化します。アイドルコールバックには 2 秒の timeout を要求しますが、実行が遅れる場合があります。対応 API がない場合は idle またはタイマーに切り替わります。この 3 戦略は有効化前のイベントを再生しません。

`hydrate=True, strategy="interaction"` は、ユーザーが `<button type="button">` をクリックすると有効化します。[サンプルページ](../../examples/basic/app/interaction/page.py)と[モジュール](../../examples/basic/islands/interaction-counter.js)を参照してください。ローダーがその island にリスナーを登録した後の対応クリックは最大 16 件を保持し、hydration 成功後に順番に再生します。超過分は警告して破棄します。リンク、フォーム送信、入力、label、編集領域は捕捉・再生しません。リスナー登録前のイベントも捕捉できません。DOM の不一致、要素の削除、有効化の失敗時には待機イベントを破棄します。再生イベントは `isTrusted=false` の合成イベントで、ブラウザーのユーザー操作が必要な動作を保証しません。island 外のキャプチャリスナーは元と再生の両方を観測する場合があります。

## クライアント側ナビゲーション

クライアントランタイムを読み込むページ（island のあるページ）では、同一オリジンのリンククリック時にフルリロードせずに遷移します。ランタイムが対象ドキュメントを取得してページ本体を差し替え、`pushState` で履歴を更新し、戻る・進むボタンも同じように動作します。リンクはホバー、キーボードフォーカス、ビューポートへの表示時にプリフェッチされます（最大 20 ページ、各 1 MiB）。200 以外の応答、HTML 以外の応答、取得エラーなど、どのような失敗でも通常のドキュメント遷移にフォールバックするため、ページが壊れることはありません。`target="_blank"` や `download` 付きのリンク、別オリジンのリンク、同一ページ内のハッシュリンクは通常のブラウザー動作のままで、JavaScript が無効の場合も同様です。`window.__pynextNav` にデバッグ用の `navigate`・`prefetch`・`isPrefetched` があります。island のないページはランタイムを読み込まないため、部分ナビゲーションは動作しません。

## Python クライアント DSL（実験的）

`pynext.client_dsl.compile_client_dsl(source)` は、Python の小さく固定された部分集合を JavaScript にコンパイルします（仕様 §7.3）。対応するもの：リテラル（JavaScript の安全な整数範囲内の int、float、str、bool、None、list、tuple、dict）、算術演算（`+ - * / // % **`）、比較、`and`/`or`/`not`、`if`/`else`、`range()` またはリストに対する `for`、`while`、関数定義、`len`、`str`、`int`、`float`、`bool`、`abs`、`min`、`max`、`list`、`print`、`.append()`、インデックス、スライス、1 句のリスト内包表記です。それ以外はすべて `ClientDSLCompileError` になります。import、リフレクション、ファイル I/O、クラス、ラムダ、`try`、f-string、`open`・`os`・`Request`・`h` のようなサーバー専用の名前はコンパイル時に拒否されるため、サーバー側のコードがブラウザバンドルに混入しません。正確な一覧と既知の意味の違い（例：`str(1.0)` の `.0` が消える）は `pynext.client_dsl.SUPPORTED` と `KNOWN_DIFFERENCES` を参照してください。[例](../../examples/basic/client_dsl_example.py)もあります。DSL は単独のコンパイラで、island バンドルへの自動組み込みはしません。

## ASGI、タイムアウト、安全性

サンプルの ASGI アプリを Uvicorn で開発モード起動します。

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

ビルド済みアプリは `create_app(project_path, production=True)` で作成します。ASGI のタイムアウトは使用するサーバーが管理します。Axum アダプターは Python の応答を最大 30 秒待ち、キャンセルを要求して 504 を返します。既にスレッド上で動作中の同期関数は 504 の後も実行を続け、書き込みなどを行う場合があります。書き込みの冪等性と、DB・外部サービス固有のタイムアウトを設定してください。プロセス分離は `--workers`（下記）で提供され、単一プロセスでは利用できません。ISR のキャンセルにも同じ制限があります。

ワーカー間の即時無効化伝播、分散レート制限、本番環境向けの安全性強化は未実装です。OpenTelemetry スパンは、任意パッケージをインストールし、トレーシングを有効化した場合に生成されます（可観測性を参照）。ASGI 適合性は `tests/test_asgi_conformance.py` で検証します（HTTP イベント順序、チャンク分割された本体、切断、失敗パスを含む lifespan）。計画と完了基準は[プロダクションロードマップ](../PRODUCTION_ROADMAP.md)を参照してください。

両アダプターのリクエスト本文は最大 1 MiB で、超過時は 413 です。Axum の既定値は本文受信 16 件、受信期限 10 秒（408）、Python 実行 64 件で、待機列は実行枠数と 64 の小さい方に制限され、満杯なら 503 です。実行数と本文設定は CLI で変更できます。HTTP/1.1 ヘッダー受信期限は 10 秒ですが、接続総数は制限しません。外部 ASGI サーバーの期限はそのサーバーで管理します。ルート以外の末尾 `/` は 308 でリダイレクトします。ビルド済み island は各 2 MiB、合計 32 MiB が上限です。

## 可観測性

構造化ログは既定で有効です。`pynext` ロガーは、1行につき `timestamp`、`level`、`message`、`request_id`、`trace_id`（`traceparent` ヘッダーを受け取った場合）、`route`、`status`、`latency_ms` を含む JSON オブジェクトを 1 つ出力します。各リクエストにはリクエスト ID が付与されます。有効な `X-Request-Id` ヘッダーがあればそれをそのまま使い、なければ新規に生成してリクエスト全体に伝播し、`x-request-id` 応答ヘッダーとして返します。ログメッセージにヘッダー、Cookie、本文、`Authorization` の値を決して含めないでください。JSON フォーマッタは認証情報らしい名前の追加フィールドを破棄し、アクセスログは生の URL ではなくルートパターン（例: `/blog/[slug]`）のみを記録します。

メトリクスはメモリ内で収集され、既定で有効です。`app.metrics.snapshot()` は、ルートパターン別のリクエスト数とステータスコード、ルート別の遅延 p50/p95/p99、Python 実行キューの長さと待機時間、Python 実行時間と Rust レンダリング時間、ISR キャッシュの hit/miss/stale と再生成の成功・失敗・開始、ワーカーの開始・タイムアウト・利用不可、応答サイズと island バンドルサイズを返します。`app.metrics.prometheus_text()` は同じカウンターを Prometheus テキスト形式で公開します。遅延サンプルはルートあたり 2048 件に制限されます。Rust が Python を経由せずに提供する新鮮な ISR ヒットはここでは数えません。Python ディスパッチに到達した ISR の結果のみが数えられます。

OpenTelemetry トレーシングは既定で無効であり、任意です。`opentelemetry-api`（および任意の SDK/エクスポーター）をインストールし、`--tracing` または `PYNEXT_TRACING=1` で有効化してください。有効な場合、`pynext.request` と `pynext.render` スパンが作成され、受信した W3C `traceparent`/`tracestate` コンテキストが伝播します。パッケージがインストールされていない場合、トレーシングは no-op で、何も壊れません。

CLI フラグ `--structured-logs` / `--no-structured-logs`、`--metrics` / `--no-metrics`、`--tracing` / `--no-tracing`、環境変数 `PYNEXT_STRUCTURED_LOGS`、`PYNEXT_METRICS`、`PYNEXT_TRACING`、`PYNEXT_LOG_LEVEL`、またはコード内で `create_app(..., observability=ObservabilityConfig(...))` により設定します。

既定の可観測性設定（構造化ログ + メトリクス有効、トレーシング無効）の測定オーバーヘッドは、ビルド済みサンプルで `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production` を各 2 回実行して測定しました。他の作業が同じ VM で並行してビルド・テスト中だったため、絶対遅延は膨らみ、実行間のばらつきが大きくなっています。有効: 25.4 / 14.2 req/s、p50 309 / 586 ms、プロセス CPU 4.79 / 5.02 秒。無効（`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`）: 18.7 / 18.5 req/s、p50 437 / 478 ms、CPU 5.01 / 4.69 秒。平均は有効 19.8 req/s・p50 447 ms に対して無効 18.6 req/s・p50 457 ms で、実行間のばらつきを超えるオーバーヘッドはありません（有効時の実行では stderr に JSON ログ約 1000 行も出力しています）。より厳密な値は静かなマシンで再測定してください。

## レート制限

レート制限はオプトインで、プロセス単位のトークンバケットです。`pynext start --rate-limit 100 --rate-limit-window 60` はクライアント IP あたり 60 秒で 100 リクエストを許可します（バーストが上限です）。制限を超えた動的リクエストは秒単位の `Retry-After` ヘッダー付きで 429 を返します。キーはクライアント IP で、`X-Forwarded-For` は `--trusted-proxy`（IP または CIDR、繰り返し指定可）で指定したピアからのものだけを信頼します。信頼できないプロキシのヘッダーは偽装可能なため無視されます。コードからは `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))` のように使います。バケットはプロセス単位で再起動すると消え、分散レート制限は未実装です。静的ファイルと新鮮な ISR ページは Python ブリッジに届く前に Rust 層が返すため、レート制限の対象外です。

転送チェーンは右から信頼済みプロキシをたどり、最も近い非信頼アドレスを使います。中間プロキシをすべて正確に登録してください。バケットはプロセスあたり最大 10,000 個で、すべて使用中なら完全に補充されるまで新しいアドレスに 429 を返します。

複数ワーカー: `pynext start --workers 4` は 1 つのポートを共有する 4 つのワーカープロセスを起動するスーパーバイザーを実行します（SO_REUSEPORT、Unix 専用、固定の `--port` が必要）。スーパーバイザーは終了したワーカーやハートビートが止まったワーカーを再起動し、SIGINT/SIGTERM を全ワーカーに転送して正常終了させます（最大 5 秒、その後 SIGKILL）。各ワーカーは独立したプロセスで、インタープリター、イベントループ、ISR キャッシュ、レート制限バケットをそれぞれ持ち、状態は共有しません。ワーカーがリクエスト処理中に死ぬとそのリクエストは失敗し、クライアントが再試行する必要があります。既定はワーカー 1 個で、従来の単一プロセスと同一です。イベントループを塞ぐハンドラーはハートビートスレッドを止めないため、詰まったハンドラーは再起動ではなく 30 秒のリクエストタイムアウトで処理されます。

## テストとトラブルシューティング

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

ルーターと HTML シリアライザには、個別の事例に加えて property-based テストもあります。Python 側は hypothesis（`tests/test_property_router_render.py`、固定シード・40 例で CI を高速に維持）、Rust 側は proptest（`crates/pynext-router` と `crates/pynext-native` の単体テスト、各 64 ケース）で、ルート照合の往復、エスケープ漏れなし、void タグ処理、任意のツリー描画を検査します。`embed` は void 一覧にありますがブロック対象タグでもあるため描画できず、対象外です。

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
- ワーカーごとに ISR の結果が異なる場合は、キャッシュバックエンドを確認してください。メモリバックエンドはプロセス単位であり、SQLite バックエンドでも各ワーカーのプロセス内コピーは共有無効化後も TTL が終わるまで残ることがあります。
