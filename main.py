import asyncio
import logging
import os
from pathlib import Path

from app.core.bot import bot, dp
from app.core.config import BOT_OWNER_ID
from app.core.db import add_admin, init_db, set_admin_level

from app.middlewares.chat_guard import ChatGuardMiddleware
from app.routers.chat_guard import router as chat_guard_router

from app.handlers.admin import router as admin_router
from app.handlers.admin_tiktok import router as admin_tiktok_router
from app.handlers.call import router as call_router
from app.handlers.broadcast import router as broadcast_router
from app.handlers.chatid import router as chatid_router
from app.handlers.collect_members import router as collect_router
from app.handlers.ping import router as ping_router
from app.handlers.start import router as start_router
from app.handlers.welcome import router as welcome_router
from app.handlers.invite import router as invite_router
from app.handlers.predict import router as predict_router
from app.handlers.profile import router as profile_router
from app.handlers.warnings import router as warnings_router
from app.handlers.talktop import router as talktop_router
from app.services.silence import run_silence_scheduler
from app.services.db_scheduler import register_tiktok_task, run_db_scheduler
from app.services.birthday_reminders import register_birthday_daily_task
from app.services.talktop import register_daily_talktop_task

logger = logging.getLogger(__name__)

HEALTH_FILE = Path("/tmp/jr-foxy-healthy")
HEALTH_HEARTBEAT_SECONDS = 30


ROUTERS = (
    chat_guard_router,  # має бути першим в списку
    welcome_router,
    invite_router,
    predict_router,
    start_router,
    profile_router,
    chatid_router,
    ping_router,
    admin_router,
    admin_tiktok_router,
    broadcast_router,
    call_router,
    talktop_router,
    collect_router,
    warnings_router,
)


def setup_middlewares() -> None:
    dp.message.middleware(ChatGuardMiddleware())
    dp.callback_query.middleware(ChatGuardMiddleware())


def setup_routers() -> None:
    for router in ROUTERS:
        dp.include_router(router)
    logger.info("routers registered", extra={"count": len(ROUTERS)})


async def run_health_heartbeat() -> None:
    while True:
        HEALTH_FILE.touch()
        await asyncio.sleep(HEALTH_HEARTBEAT_SECONDS)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    HEALTH_FILE.unlink(missing_ok=True)
    setup_middlewares()
    setup_routers()

    await init_db()
    await add_admin(BOT_OWNER_ID)
    await set_admin_level(BOT_OWNER_ID, 4)
    await register_tiktok_task()
    await register_birthday_daily_task()
    await register_daily_talktop_task()

    me = await bot.get_me()
    HEALTH_FILE.touch()
    logger.info(
        "starting polling",
        extra={
            "bot_id": me.id,
            "username": me.username,
            "version": os.getenv("BOT_VERSION", "dev"),
        },
    )

    health_task = asyncio.create_task(run_health_heartbeat())
    silence_task = asyncio.create_task(run_silence_scheduler(bot))
    db_sched_task = asyncio.create_task(run_db_scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        health_task.cancel()
        silence_task.cancel()
        db_sched_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
