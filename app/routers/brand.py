from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from app.utils.brand import brand_for_headers

router = APIRouter(tags=["Brand"], prefix="/api/brand")


@router.get("", response_model=dict | None)
async def get_brand(request: Request):
    """White-label brand of the requested host (null = default). Public: the login page needs it."""
    brand = brand_for_headers(request.headers)
    return brand.public() if brand else None


@router.get("/asset/{file_name}", include_in_schema=False)
async def get_brand_asset(request: Request, file_name: str):
    brand = brand_for_headers(request.headers)
    path = brand.asset_path(file_name) if brand else None
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})
