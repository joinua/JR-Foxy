import asyncio

from app.core.bot import bot, dp
from app.core.db import init_db
from app.handlers.start import router as start_router
from app.handlers.chatid import router as chatid_router
from app.handlers.ping import router as ping_router
from app.handlers.call import router as call_router
from app.handlers.collect_members import router as collect_router




async def main():
    await init_db()

    dp.include_router(start_router)
    dp.include_router(chatid_router)
    dp.include_router(ping_router)
    dp.include_router(call_router)
    dp.include_router(collect_router)
    

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
