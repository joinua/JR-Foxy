"""Profile command router."""

from aiogram import Router

from app.handlers.profile.admin_commands import router as admin_commands_router
from app.handlers.profile.edit_commands import router as edit_commands_router
from app.handlers.profile.profile import router as profile_router
from app.handlers.profile.profile_admin import router as profile_admin_router

router = Router()
router.include_router(profile_router)
router.include_router(edit_commands_router)
router.include_router(admin_commands_router)
router.include_router(profile_admin_router)
