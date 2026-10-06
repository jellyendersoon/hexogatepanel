import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.settings import get_settings, modify_settings
from app.db.models import Settings
from app.models.settings import SECRET_MASK, General, SettingsSchema, Subscription
from app.nats.message import MessageTopic
from app.nats.router import router
from app.notification.client import define_client
from app.settings import refresh_caches
from app.telegram import startup_telegram_bot

from . import BaseOperation


def _mask_secrets(settings: SettingsSchema) -> SettingsSchema:
    """Return a copy with stored secrets replaced by SECRET_MASK."""
    masked = settings.model_copy(deep=True)
    if masked.telegram:
        if masked.telegram.token:
            masked.telegram.token = SECRET_MASK
        if masked.telegram.webhook_secret:
            masked.telegram.webhook_secret = SECRET_MASK
    if masked.notification_settings and masked.notification_settings.telegram_api_token:
        masked.notification_settings.telegram_api_token = SECRET_MASK
    if masked.webhook and masked.webhook.webhooks:
        for hook in masked.webhook.webhooks:
            if hook.secret:
                hook.secret = SECRET_MASK
    return masked


def _restore_masked_secrets(modify: SettingsSchema, current: SettingsSchema) -> SettingsSchema:
    """Replace SECRET_MASK values in an update with the currently stored secrets."""
    if modify.telegram and current.telegram:
        if modify.telegram.token == SECRET_MASK:
            modify.telegram.token = current.telegram.token
        if modify.telegram.webhook_secret == SECRET_MASK:
            modify.telegram.webhook_secret = current.telegram.webhook_secret
    if (
        modify.notification_settings
        and current.notification_settings
        and modify.notification_settings.telegram_api_token == SECRET_MASK
    ):
        modify.notification_settings.telegram_api_token = current.notification_settings.telegram_api_token
    if modify.webhook and modify.webhook.webhooks:
        current_secrets = {hook.url: hook.secret for hook in (current.webhook.webhooks if current.webhook else [])}
        for hook in modify.webhook.webhooks:
            if hook.secret == SECRET_MASK:
                if hook.url not in current_secrets:
                    raise ValueError(f"A secret is required for the new webhook {hook.url}")
                hook.secret = current_secrets[hook.url]
    return modify


class SettingsOperation(BaseOperation):
    @staticmethod
    async def reset_services(old_settings: SettingsSchema, new_settings: SettingsSchema):
        if new_settings.telegram != old_settings.telegram:
            await startup_telegram_bot()
        # When webhooks are disabled, send_notifications() already returns early
        # Pending webhook notifications will be processed when webhooks are re-enabled
        if old_settings.notification_settings.proxy_url != new_settings.notification_settings.proxy_url:
            await define_client()

    async def get_settings(self, db: AsyncSession) -> Settings:
        return await get_settings(db)

    async def get_settings_masked(self, db: AsyncSession) -> SettingsSchema:
        """Settings for the API: bot tokens and webhook secrets are masked."""
        return _mask_secrets(SettingsSchema.model_validate(await get_settings(db)))

    async def modify_settings(self, db: AsyncSession, modify: SettingsSchema) -> SettingsSchema:
        db_settings = await get_settings(db)
        old_settings = SettingsSchema.model_validate(db_settings)

        try:
            modify = _restore_masked_secrets(modify, old_settings)
        except ValueError as exc:
            await self.raise_error(message=str(exc), code=422)

        if modify.general and modify.general.custom_variables is not None:
            subscription = modify.subscription or Subscription.model_validate(db_settings.subscription)
            modify.subscription = subscription.model_copy(update={"custom_variables": modify.general.custom_variables})
            modify.general = modify.general.model_copy(update={"custom_variables": None})

        db_settings = await modify_settings(db, db_settings, modify)
        new_settings = SettingsSchema.model_validate(db_settings)
        if new_settings.general and new_settings.subscription:
            new_settings.general.custom_variables = new_settings.subscription.custom_variables

        await refresh_caches()
        # Publish settings update via NATS (all workers will refresh their caches)
        await router.publish(MessageTopic.SETTING, {"action": "refresh"})
        asyncio.create_task(self.reset_services(old_settings, new_settings))

        return _mask_secrets(new_settings)

    async def get_general_settings(self, db: AsyncSession):
        settings = await self.get_settings(db)
        general = General.model_validate(settings.general)
        subscription = Subscription.model_validate(settings.subscription)
        return general.model_copy(update={"custom_variables": subscription.custom_variables})
