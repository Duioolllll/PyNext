# PyNext マニュアル — 日本語

[English](../en/manual.md) · [한국어](../ko/manual.md) · [简体中文](../zh-CN/manual.md)

このマニュアルは **v0.6.0a1** を対象とします。PyNext は Python と Rust で構築する実験的なフルスタックフレームワークです。Next.js、React、React Server Components との互換性はありません。現時点ではインターネットに直接公開したり、信頼できないアプリを実行したりしないでください。[変更履歴](../../CHANGELOG.md)と[今後の計画](../PRODUCTION_ROADMAP.md)も参照してください。

## 必要な環境と起動

CPython 3.13、Rust/Cargo、[uv](https://docs.astral.sh/uv/) が必要です。その他の Python バージョンやデプロイ環境は十分に検証されていません。サンプルは island を使うため、esbuild 0.28.1 のインストールに Node.js 18 以降と npm も必要です。

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout v0.6.0a1
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

http://127.0.0.1:3000/、/about、/guides/intro、/news/example、/hydrate、/strategies、/api/users にアクセスできます。Ctrl+C で終了します。

`pynext dev --dir examples/basic` はビルドなしでソースを提供しますが、**ファイル変更の自動反映はない**ため変更後に再起動してください。`pynext start` はビルド manifest を使い、追跡対象のソースが変わると再ビルドを要求します。Island を使わないアプリには Node と esbuild は不要です。上記はリポジトリからのインストールであり、PyPI 公開を意味しません。

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

`page.py` はデコレーター付きの `Page`、`route.py` は HTTP ハンドラーをエクスポートします。`layout.py` は配下のページを囲みます。優先順位は静的パス > `[slug]` > `[...path]` です。単一パラメーターは文字列、catch-all は文字列のリストです。省略可能な catch-all、ルートグループ、並列・インターセプトルート、クライアント側ナビゲーションは未実装です。

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` で HTML ツリーを作成し、文字列はエスケープされます。SSR はレスポンス全体をバッファーして返し、任意の raw HTML を挿入する API はありません。

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

## SSR、SSG、ISR

- `@page()` はリクエストごとにレンダリングします。
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

SSG と ISR のページ関数は `request` を受け取れません。公開コンテンツにのみ使用し、環境変数や外部データが共有可能か確認してください。ISR は匿名リクエストのクエリ文字列をキャッシュキーに含めません。Cookie または Authorization があるリクエストは共有 ISR キャッシュを迂回し、`private, no-store` で返します。キャッシュは上限付きの**プロセス内メモリ**であり、ワーカー間で共有・無効化されません。SSG の内容を更新するには再ビルドが必要です。

SSG ビルドの上限は 10,000 ページで、開発モードでは SSG をリクエストごとにレンダリングします。ISR ページは `generate_static_params()` をエクスポートできず、同時生成はプロセスごとに最大 128 件です。

## 公開ファイルと islands

`public/styles/site.css` は `/styles/site.css` として公開されます。ビルド時にスナップショットと整合性検証を行いますが、本番起動時にも元ファイルが必要です。隠しファイル、シンボリックリンク、衝突するパスは拒否されます。開発モードで公開ファイルを変更した場合もサーバーを再起動してください。

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

有効化の既定値は `load` です。`visible` は画面内に入った時、`idle` はアイドルコールバックを要求して有効化します。アイドルコールバックには 2 秒の timeout を要求しますが、実行が遅れる場合があります。対応 API がない場合は idle またはタイマーに切り替わります。有効化前のクリック・入力イベントは**再生されません**。

## ASGI、タイムアウト、安全性

サンプルの ASGI アプリを Uvicorn で開発モード起動します。

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

ビルド済みアプリは `create_app(project_path, production=True)` で作成します。ASGI のタイムアウトは使用するサーバーが管理します。Axum アダプターは Python の応答を最大 30 秒待ち、キャンセルを要求して 504 を返します。既にスレッド上で動作中の同期関数は 504 の後も実行を続け、書き込みなどを行う場合があります。書き込みの冪等性と、DB・外部サービス固有のタイムアウトを設定してください。強制的なプロセス分離はなく、ISR のキャンセルにも同じ制限があります。

ストリーミング、WebSocket、分散 ISR、分散レート制限、OpenTelemetry、本番環境向けの安全性強化は未実装です。計画と完了基準は[プロダクションロードマップ](../PRODUCTION_ROADMAP.md)を参照してください。

両アダプターのリクエスト本文は最大 1 MiB で、超過時は 413 を返します。Axum は本文受信を最大 64 件、全体の受信時間を 10 秒に制限し、時間超過時は 408 を返します。本文受信または Python 実行枠が満杯の場合は 503 です。本文のない静的ファイルと有効な ISR キャッシュは Python 実行枠を消費しません。この期限は HTTP ヘッダーの受信時間や接続総数を制限しません。外部 ASGI サーバーの期限はそのサーバーで管理します。ルート以外の末尾 `/` は 308 でリダイレクトします。ビルド済み island は各 2 MiB、合計 32 MiB が上限です。

## テストとトラブルシューティング

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

サンプルをビルドした後、Chromium で検証します。

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

`uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4` で小規模な閉ループ負荷テストを実行できます。全試行と成功した 2xx リクエストの秒間処理数、エラー・ステータス、全試行と成功分の p50/p95/p99、応答サイズ、CPU、最大 RSS を分けて記録します。既定モードの CPU とプロセス生存期間中の最大 RSS にはサーバーと負荷生成側の両方が含まれます。別のローカルサーバーには `--base-url http://127.0.0.1:3000` を指定し、Linux では `--server-pid <PID>` でそのプロセスとスレッドの CPU を個別に測定できます。子ワーカーは含みません。CI は本番モード（`--production`、ビルド済みサンプルを使用）で測定した小規模負荷の結果を保存しますが、性能・メモリ回帰の自動しきい値はまだありません。

- ソースの変更を指摘されたら `pynext build` を再実行します。
- esbuild が見つからない場合はリポジトリのルートで `npm ci` を実行するか、`PYNEXT_ESBUILD` に実行ファイルを指定します。
- Windows では npm の esbuild を Node 経由で実行します。`PYNEXT_ESBUILD` を直接設定する場合は、`.cmd`・`.bat` ではなくネイティブの `esbuild.exe` を指定してください。
- 動的 SSG パスが 404 の場合、`generate_static_params()` に追加して再ビルドします。
- Hydration が一致しない場合は、サーバー fallback とクライアント `render` の結果を決定的に一致させてください。
- ワーカーごとに ISR の結果が異なる場合は、キャッシュがプロセス単位であることを確認してください。
