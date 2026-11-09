# Next.js source attribution

The route trie, precedence and conflict checks in `crates/pynext-router` are
adapted from the Next.js `sorted-routes.ts` implementation and unit tests at
commit `6100e81e314f7aad0c94e189bbae87820d7efb37`:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/shared/lib/router/utils/sorted-routes.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/test/unit/page-route-sorter.test.ts

The route-specificity comparator in `python/pynext/sortable_routes.py` (and
its ported cases in `tests/test_sortable_routes.py`) is translated from
`sortable-routes.ts` at commit `0cf37690` — the revision the Next.js/PyNext
comparison report used; the pinned commit above could not be checked for
this file from the shallow clone:

- https://github.com/vercel/next.js/blob/0cf37690/packages/next/src/shared/lib/router/utils/sortable-routes.ts
- https://github.com/vercel/next.js/blob/0cf37690/packages/next/src/shared/lib/router/utils/sortable-routes.test.ts

The static parameter shape and path validation were also informed by the
following source. PyNext's implementation is independent and supports a
smaller grammar and a fixed 404 fallback for paths not generated at build time:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/build/static-paths/pages.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/build/static-paths/app.ts

Public-file precedence and conflict handling were informed by:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/server/lib/router-utils/filesystem.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/server/lib/router-server.ts

ISR freshness, stale serving, and single-flight regeneration were informed by:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/server/response-cache/index.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/server/lib/incremental-cache/index.ts

The explicit separation of server code and a client module manifest was
informed by Next.js client-boundary handling. PyNext's island mount protocol is
independent and does not implement React Server Components or React hydration:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/build/webpack/loaders/next-flight-loader/index.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/build/webpack/plugins/flight-manifest-plugin.ts

The hydration lifecycle was informed by Next.js's client entrypoint, which
uses React's hydration APIs. PyNext uses its own explicit DOM comparison and
island contract; it is not React hydration or React Server Components:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/client/app-index.tsx

Visibility observation and idle scheduling were informed by these Next.js
client utilities. PyNext applies the concepts to its own island activation:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/client/use-intersection.tsx
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/client/request-idle-callback.ts

PyNext supports a deliberately smaller route grammar; see the README. It does
not include or implement Next.js, React, or the React Server Components protocol.

The MIT License (MIT)

Copyright (c) 2025 Vercel, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
