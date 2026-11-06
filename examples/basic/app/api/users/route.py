from pynext import JSONResponse, route

@route("GET")
async def users():
    return JSONResponse({"users": ["Alice", "Bob"]})

@route("POST")
async def create_user(request):
    data = await request.json()
    return JSONResponse({"name": data["name"]}, status=201)
