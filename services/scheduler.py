import logging
import asyncio
import json
from datetime import date, datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from database.db import async_session
from database.models import User, Ticket, ScheduledPost
from services.sheets import sync_user_data
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from config import PRIVATE_CHANNEL_ID

async def check_expired_subscriptions(bot: Bot):
    today = date.today()
    if today.day != 1:
        return # Страховка, чтобы скрипт выполнялся строго 1-го числа

    async with async_session() as session:
        # 1. Переводим в статус Expired тех, у кого закончилась дата подписки
        result = await session.execute(
            select(User).where(User.subscription_status == "Active", User.expire_date < today)
        )
        expired_users = result.scalars().all()

        for user in expired_users:
            user.subscription_status = "Expired"
            try:
                await bot.send_message(user.telegram_id, "Ваша подписка истекла! Пожалуйста, продлите её, чтобы участвовать в новом розыгрыше.")
            except Exception as e:
                logging.error(f"Failed to notify user {user.telegram_id}: {e}")
            
            asyncio.create_task(sync_user_data(user.telegram_id))

        # 2. ПОЛНОЕ СЖИГАНИЕ ВСЕХ БИЛЕТОВ (Без исключений)
        from sqlalchemy import delete
        tickets_result = await session.execute(
            select(Ticket).where(Ticket.status == "Active")
        )
        tickets_to_burn = tickets_result.scalars().all()
        count = len(tickets_to_burn)
        
        await session.execute(delete(Ticket))
        await session.commit()
        logging.info(f"Автоматическое списание завершено. Аннулировано билетов: {count}")

async def reminder_subscriptions(bot: Bot):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.subscription_status == "Active")
        )
        users = result.scalars().all()
        for user in users:
            try:
                await bot.send_message(user.telegram_id, "Напоминаем, что текущий месяц подходит к концу. Не забудьте продлить подписку на следующий месяц!")
            except Exception as e:
                logging.error(f"Failed to send reminder to user {user.telegram_id}: {e}")

async def publish_post_to_channel(bot: Bot, post: ScheduledPost) -> bool:
    try:
        reply_markup = None
        if post.buttons_json:
            try:
                buttons_data = json.loads(post.buttons_json)
                if buttons_data:
                    inline_keyboard = []
                    for b in buttons_data:
                        inline_keyboard.append([InlineKeyboardButton(text=b['text'], url=b['url'])])
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            except Exception as e:
                logging.error(f"Error parsing buttons_json for post {post.id}: {e}")

        if post.media_type == "photo":
            await bot.send_photo(
                chat_id=PRIVATE_CHANNEL_ID,
                photo=post.media_file_id,
                caption=post.text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        elif post.media_type == "video":
            await bot.send_video(
                chat_id=PRIVATE_CHANNEL_ID,
                video=post.media_file_id,
                caption=post.text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        elif post.media_type == "album":
            media_items = json.loads(post.media_file_id)
            media_group = []
            for idx, item in enumerate(media_items):
                file_id = item.get("file_id")
                m_type = item.get("type", "photo")
                cap = post.text if idx == 0 else None
                parse_m = "HTML" if idx == 0 else None
                if m_type == "video":
                    media_group.append(InputMediaVideo(media=file_id, caption=cap, parse_mode=parse_m))
                else:
                    media_group.append(InputMediaPhoto(media=file_id, caption=cap, parse_mode=parse_m))
            await bot.send_media_group(chat_id=PRIVATE_CHANNEL_ID, media=media_group)
            if reply_markup:
                await bot.send_message(
                    chat_id=PRIVATE_CHANNEL_ID,
                    text=post.text or "Ссылки:",
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
        else:
            await bot.send_message(
                chat_id=PRIVATE_CHANNEL_ID,
                text=post.text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        return True
    except Exception as e:
        logging.error(f"Error publishing post {post.id} to channel {PRIVATE_CHANNEL_ID}: {e}")
        return False

async def check_and_publish_scheduled_posts(bot: Bot):
    if PRIVATE_CHANNEL_ID == 0:
        return

    now = datetime.now()
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.status == "Pending",
                ScheduledPost.publish_at <= now
            )
        )
        posts_to_publish = result.scalars().all()

        for post in posts_to_publish:
            success = await publish_post_to_channel(bot, post)
            post.status = "Sent" if success else "Failed"
            
        if posts_to_publish:
            await session.commit()

def setup_scheduler(bot: Bot):
    scheduler = AsyncIOScheduler()
    
    # Запуск проверки и сжигания 1-го числа каждого месяца в 00:01
    # scheduler.add_job(check_expired_subscriptions, 'cron', day=1, hour=0, minute=1, args=[bot])
    
    # Напоминание об оплате 28-го числа в 10:00
    # scheduler.add_job(reminder_subscriptions, 'cron', day=28, hour=10, minute=0, args=[bot])

    # Проверка отложенных постов каждую минуту
    scheduler.add_job(check_and_publish_scheduled_posts, 'interval', minutes=1, args=[bot])

    scheduler.start()
    return scheduler