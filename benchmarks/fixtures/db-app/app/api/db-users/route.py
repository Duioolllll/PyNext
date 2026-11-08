import asyncio

from pynext import JSONResponse, route


@route("GET")
async def db_users(request):
    """General DB query: point lookup by primary key (see resources.py)."""
    raw = request.query.get("id", ["42"])[0]
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return JSONResponse({"error": "id must be an integer"}, status=400)
    row = await asyncio.to_thread(request.resources["db"].query, user_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status=404)
    return JSONResponse(row)
