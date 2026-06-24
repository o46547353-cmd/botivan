import asyncio
import logging
from aiogram import Bot, Dispatcher
from database.db import init_db
from handlers.user import router as user_router
from handlers.admin import router as admin_router
from services.scheduler import setup_scheduler
import config

from middlewares.ban import BanMiddleware

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    
    # Init database
    from database.db import cleanup_and_align_tickets
    await init_db()
    await cleanup_and_align_tickets()
    
    # Load dynamic admins cache
    from handlers.admin import load_dynamic_admins
    await load_dynamic_admins()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    # Register middleware
    dp.update.outer_middleware(BanMiddleware())
    from middlewares.album import AlbumMiddleware
    dp.message.outer_middleware(AlbumMiddleware())

    # Include routers
    dp.include_router(user_router)
    dp.include_router(admin_router)

    # Setup APScheduler
    setup_scheduler(bot)

    print("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")