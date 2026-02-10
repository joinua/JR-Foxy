import asyncio
import logging

from app.core.bot import bot, dp
from app.core.config import BOT_OWNER_ID
from app.core.db import add_admin, init_db, set_admin_level

from app.middlewares.chat_guard import ChatGuardMiddleware
from app.routers.chat_guard import router as chat_guard_router

from app.handlers.admin import router as admin_router
from app.handlers.call import router as call_router
from app.handlers.broadcast import router as broadcast_router
from app.handlers.chatid import router as chatid_router
from app.handlers.collect_members import router as collect_router
from app.handlers.ping import router as ping_router
from app.handlers.start import router as start_router
from app.handlers.welcome import router as welcome_router
from app.handlers.predict import router as predict_router
from app.handlers.warnings import router as warnings_router

logger = logging.getLogger(__name__)


ROUTERS = (
    chat_guard_router,  # має бути першим в списку
    welcome_router,
    predict_router,
    start_router,
    chatid_router,
    ping_router,
    admin_router,
    broadcast_router,
    call_router,
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    setup_middlewares()
    setup_routers()

    await init_db()
    await add_admin(BOT_OWNER_ID)
    await set_admin_level(BOT_OWNER_ID, 4)

    me = await bot.get_me()
    logger.info(
        "starting polling",
        extra={"bot_id": me.id, "username": me.username},
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
