from fastapi import APIRouter

from . import (
    admin,
    admin_role,
    api_key,
    brand,
    client_template,
    core,
    fleet_live,
    group,
    home,
    host,
    hwid,
    node,
    settings,
    setup,
    subscription,
    system,
    user,
    user_template,
)

api_router = APIRouter()

routers = [
    home.router,
    brand.router,
    admin.router,
    api_key.router,
    admin_role.router,
    setup.router,
    system.router,
    settings.router,
    group.router,
    core.router,
    client_template.router,
    host.router,
    node.router,
    fleet_live.router,
    user.router,
    subscription.router,
    user_template.router,
    hwid.router,
]

for router in routers:
    api_router.include_router(router)

__all__ = ["api_router"]
