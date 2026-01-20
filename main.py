import asyncio

from app.core.bot import bot, dp
from app.core.db import init_db
from app.handlers.call import router as call_router
from app.handlers.chatid import router as chatid_router
from app.handlers.collect_members import router as collect_router
from app.handlers.ping import router as ping_router
from app.handlers.start import router as start_router
from app.core.db import ensure_admins_table
await ensure_admins_table()


ROUTERS = (
    start_router,
    chatid_router,
    ping_router,
    call_router,
    collect_router,
)


async def main() -> None:
    await init_db()

    for router in ROUTERS:
        dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
