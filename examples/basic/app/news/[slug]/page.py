from datetime import datetime, timezone
from pynext import h, island, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]),
             h("p", {}, "Generated at " + datetime.now(timezone.utc).isoformat(timespec="seconds")),
             island("counter", props={"initial": 10}, fallback=h("p", {}, "Enable JavaScript to use the counter.")),
             island("hydrated-counter", props={"initial": 4}, hydrate=True,
                    fallback=h("button", {}, "Hydrated: 4")))
