from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, Update
from database.db import async_session
from database.models import User
from sqlalchemy import select

from services.sheets import sync_user_data
import asyncio

class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user_id = None
        user_event = None

        if isinstance(event, Update):
            if event.message:
                user_id = event.message.from_user.id
                user_event = event.message
            elif event.callback_query:
                user_id = event.callback_query.from_user.id
                user_event = event.callback_query
        elif isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
            user_event = event

        if user_id:
            async with async_session() as session:
                res = await session.execute(select(User).where(User.telegram_id == user_id))
                user = res.scalars().first()
                if not user:
                    user = User(telegram_id=user_id)
                    session.add(user)
                    await session.commit()
                    asyncio.create_task(sync_user_data(user_id))
                elif user.is_banned:
                    if isinstance(user_event, Message):
                        await user_event.answer("🚫 <b>Вы заблокированы в этом боте.</b>", parse_mode="HTML")
                    elif isinstance(user_event, CallbackQuery):
                        await user_event.answer("Вы заблокированы.", show_alert=True)
                    return
        return await handler(event, data)
