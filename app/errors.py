from fastapi.responses import JSONResponse, Response


def err(http_status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=http_status)


def bare_404() -> Response:
    return Response(status_code=404)
