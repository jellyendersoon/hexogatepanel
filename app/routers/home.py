from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse

from app.templates import render_template
from app.utils.brand import brand_for_headers
from config import dashboard_settings, template_settings

DASHBOARD_ROUTE = dashboard_settings.path.rstrip("/")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def base(request: Request):
    brand = brand_for_headers(request.headers)
    return render_template(template_settings.home_page_template, {"brand": brand.template_context() if brand else None})


@router.get("/health", response_model=dict, status_code=status.HTTP_200_OK)
async def health():
    return {"status": "ok"}
