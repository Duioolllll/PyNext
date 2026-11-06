from datetime import datetime, timezone
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]), h("p", {}, "Generated at " + datetime.now(timezone.utc).isoformat(timespec="seconds")))
