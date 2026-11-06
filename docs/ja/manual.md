# PyNext v0.2.0 — ja

保存された開発ビルドから再構成した実験版で、後のリクエスト検証修正をバックポートしています。Next.js・React とは互換ではありません。インターネットに直接公開しないでください。

要件: CPython 3.13、Rust/Cargo、uv。 Island のビルドには Node.js 18 以降と npm が必要です。ビルド前に `npm ci` を実行してください。

**Features / 기능 / 機能 / 功能:** ファイルルーティング、SSR、SSG、公開ファイル、プロセスごとの ISR, JavaScript/TypeScript island の mount.

未対応: hydration、遅延アクティベーション。

~~~bash
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

SSG/ISR は公開データ用で、ISR キャッシュはプロセスごとです。Axum は 30 秒後に 504 を返しますが、実行中の同期関数は継続する場合があります。WebSocket、ストリーミング、分散 ISR は未実装です。

~~~python
from pynext import h, page

@page()
def Page():
    return h("h1", {}, "Hello PyNext")
~~~
