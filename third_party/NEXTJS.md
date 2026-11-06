# Next.js source attribution

The route trie, precedence and conflict checks in `crates/pynext-router` are
adapted from the Next.js `sorted-routes.ts` implementation and unit tests at
commit `6100e81e314f7aad0c94e189bbae87820d7efb37`:

- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/packages/next/src/shared/lib/router/utils/sorted-routes.ts
- https://github.com/vercel/next.js/blob/6100e81e314f7aad0c94e189bbae87820d7efb37/test/unit/page-route-sorter.test.ts

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
