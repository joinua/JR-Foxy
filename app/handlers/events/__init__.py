"""Event feature router."""

from aiogram import Router

from app.handlers.events.admin import router as admin_router
from app.handlers.events.public import router as public_router
from app.handlers.events.review import router as review_router
from app.handlers.events.reliability import router as reliability_router


router = Router()
router.include_router(admin_router)
router.include_router(public_router)
router.include_router(review_router)
router.include_router(reliability_router)
