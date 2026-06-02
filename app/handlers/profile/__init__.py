"""Profile command router."""

from aiogram import Router

from app.handlers.profile.edit_commands import router as edit_commands_router
from app.handlers.profile.profile import router as profile_router

router = Router()
router.include_router(profile_router)
router.include_router(edit_commands_router)
