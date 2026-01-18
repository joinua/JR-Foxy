import asyncio

from app.core.bot import bot, dp
from app.handlers.start import router as start_router
from app.handlers.chatid import router as chatid_router
from app.handlers.ping import router as ping_router

async def main():
    dp.include_router(start_router)
    dp.include_router(chatid_router)
    dp.include_router(ping_router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
