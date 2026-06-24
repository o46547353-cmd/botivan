import logging
import calendar
import os
from datetime import date, datetime, time, timedelta
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.filters import Command
from services.media import send_payment_receipt_to_admin
from aiogram.fsm.context import FSMContext
from database.db import async_session
from database.models import User, SubscriptionRequest, PaymentConfig, Ticket, ScheduledPost, AdminUser, BotConfig
from states.states import AdminStates
import json
from sqlalchemy import select, func
from services.tickets import generate_tickets, get_tickets_counter_text, get_tier_tickets, get_all_tier_tickets
from services.sheets import sync_user_data
import asyncio
from config import ADMIN_IDS, PRIVATE_CHANNEL_ID
from aiogram import BaseMiddleware

router = Router()

DYNAMIC_ADMIN_IDS = set()
DYNAMIC_ADMIN_USERNAMES = set()

async def load_dynamic_admins():
    global DYNAMIC_ADMIN_IDS, DYNAMIC_ADMIN_USERNAMES
    async with async_session() as session:
        res = await session.execute(select(AdminUser))
        admins = res.scalars().all()
        DYNAMIC_ADMIN_IDS = {a.telegram_id for a in admins if a.telegram_id is not None}
        DYNAMIC_ADMIN_USERNAMES = {a.username.lower() for a in admins if a.username}

def is_admin(telegram_id: int, username: str = None) -> bool:
    if telegram_id in ADMIN_IDS:
        return True
    if telegram_id in DYNAMIC_ADMIN_IDS:
        return True
    if username and username.lower().lstrip('@') in DYNAMIC_ADMIN_USERNAMES:
        return True
    return False

class AdminMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message | CallbackQuery, data: dict):
        user_id = event.from_user.id
        username = event.from_user.username
        
        # Если юзернейм админа есть в кэше, но ID еще не записан - обновим его
        if username and username.lower() in DYNAMIC_ADMIN_USERNAMES and user_id not in DYNAMIC_ADMIN_IDS:
            DYNAMIC_ADMIN_IDS.add(user_id)
            asyncio.create_task(update_admin_id_db(user_id, username))

        if is_admin(user_id, username):
            return await handler(event, data)
        
        if isinstance(event, CallbackQuery):
            await event.answer("У вас нет прав администратора.", show_alert=True)
        return

async def update_admin_id_db(telegram_id: int, username: str):
    async with async_session() as session:
        res = await session.execute(select(AdminUser).where(func.lower(AdminUser.username) == username.lower().lstrip('@')))
        admin = res.scalars().first()
        if admin and not admin.telegram_id:
            admin.telegram_id = telegram_id
            await session.commit()

router.message.outer_middleware(AdminMiddleware())
router.callback_query.outer_middleware(AdminMiddleware())

def get_end_of_month(current_date: date) -> date:
    if current_date.day > 25:
        if current_date.month == 12:
            target_year = current_date.year + 1
            target_month = 1
        else:
            target_year = current_date.year
            target_month = current_date.month + 1
    else:
        target_year = current_date.year
        target_month = current_date.month

    _, last_day = calendar.monthrange(target_year, target_month)
    return date(target_year, target_month, last_day)

def admin_main_keyboard(sales_stopped: bool = False, free_ticket_enabled: bool = True, buy_ticket_enabled: bool = True):
    toggle_text = "🟢 Вкл. продажи" if sales_stopped else "🛑 Стоп-продажи"
    toggle_free_text = "🔴 Скрыть бесплатный билет" if free_ticket_enabled else "🟢 Показать бесплатный билет"
    toggle_buy_text = "🔴 Скрыть купить билет" if buy_ticket_enabled else "🟢 Показать купить билет"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Заявки", callback_data="admin_pending_reqs"),
            InlineKeyboardButton(text="📅 Планировщик", callback_data="admin_scheduler_menu")
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⚙️ Настройка оплаты", callback_data="edit_payment_config")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
            InlineKeyboardButton(text="📅 По датам", callback_data="admin_period_stats")
        ],
        [
            InlineKeyboardButton(text="🔍 Поиск юзера", callback_data="admin_search_user"),
            InlineKeyboardButton(text="🎁 Выдать билеты", callback_data="admin_give_tickets")
        ],
        [
            InlineKeyboardButton(text="🗂 Экспорт билетов", callback_data="admin_export_tickets"),
            InlineKeyboardButton(text="🔥 Сброс билетов", callback_data="admin_reset_tickets_confirm")
        ],
        [
            InlineKeyboardButton(text="👤 Админы", callback_data="admin_manage_admins"),
            InlineKeyboardButton(text="✉️ Приветствие", callback_data="admin_edit_welcome")
        ],
        [
            InlineKeyboardButton(text="🎥 Инструкция", callback_data="admin_edit_instruction"),
            InlineKeyboardButton(text="🔑 Инвайт-ссылка", callback_data="admin_gen_invite")
        ],
        [
            InlineKeyboardButton(text="📊 Экспорт CSV", callback_data="admin_export_csv"),
            InlineKeyboardButton(text="⚙️ Настройка билетов", callback_data="admin_edit_tier_tickets")
        ],
        [
            InlineKeyboardButton(text="⚙️ Лимит билетов", callback_data="admin_edit_tickets_limit"),
            InlineKeyboardButton(text=toggle_text, callback_data="admin_toggle_sales")
        ],
        [
            InlineKeyboardButton(text=toggle_buy_text, callback_data="admin_toggle_buy_ticket")
        ]
    ])

def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with async_session() as session:
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        sales_stopped = sales_cfg.value == "true" if sales_cfg else False
        
        free_res = await session.execute(select(BotConfig).where(BotConfig.key == "free_ticket_enabled"))
        free_cfg = free_res.scalars().first()
        free_ticket_enabled = free_cfg.value != "false" if free_cfg else True

        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        buy_ticket_enabled = buy_cfg.value != "false" if buy_cfg else True
        
    text = (
        "👑 <b>Панель администратора</b>\n"
        "───────────────────\n"
        "Добро пожаловать! Здесь вы можете управлять заявками на подписку, настраивать автопостинг, выгружать статистику и управлять билетами.\n\n"
        "Выберите необходимое действие ниже:"
    )
    await message.answer(text, reply_markup=admin_main_keyboard(sales_stopped, free_ticket_enabled, buy_ticket_enabled), parse_mode="HTML")

@router.callback_query(F.data == "back_to_admin")
async def back_to_admin_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    async with async_session() as session:
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        sales_stopped = sales_cfg.value == "true" if sales_cfg else False
        
        free_res = await session.execute(select(BotConfig).where(BotConfig.key == "free_ticket_enabled"))
        free_cfg = free_res.scalars().first()
        free_ticket_enabled = free_cfg.value != "false" if free_cfg else True

        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        buy_ticket_enabled = buy_cfg.value != "false" if buy_cfg else True
        
    text = (
        "👑 <b>Панель администратора</b>\n"
        "───────────────────\n"
        "Добро пожаловать! Здесь вы можете управлять заявками на подписку, настраивать автопостинг, выгружать статистику и управлять билетами.\n\n"
        "Выберите необходимое действие ниже:"
    )
    await callback.message.answer(text, reply_markup=admin_main_keyboard(sales_stopped, free_ticket_enabled, buy_ticket_enabled), parse_mode="HTML")
    await callback.answer()


# --- РУЧНОЕ СЖИГАНИЕ БИЛЕТОВ ---
@router.callback_query(F.data == "admin_reset_tickets_confirm")
async def confirm_reset_tickets(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, СБРОСИТЬ ВСЕ БИЛЕТЫ", callback_data="admin_reset_tickets_do")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_admin")]
    ])
    
    text = (
        "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        "Вы собираетесь аннулировать <b>ВСЕ активные билеты</b> у всех пользователей прямо сейчас.\n\n"
        "Вы уверены?"
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_reset_tickets_do")
async def do_reset_tickets(callback: CallbackQuery):
    await callback.message.edit_text("⏳ Идет сброс билетов, подождите...")
    
    async with async_session() as session:
        from sqlalchemy import delete
        result = await session.execute(select(Ticket).where(Ticket.status == "Active"))
        active_tickets = result.scalars().all()
        count = len(active_tickets)
        
        await session.execute(delete(Ticket))
        await session.commit()
        
    await callback.message.answer(
        f"✅ <b>Успешно!</b>\nАннулировано билетов: {count}.\nБаза билетов очищена.", 
        reply_markup=admin_back_keyboard(), 
        parse_mode="HTML"
    )
    await callback.answer()
# -------------------------------


@router.callback_query(F.data == "admin_export_tickets")
async def export_all_tickets(callback: CallbackQuery):
    await callback.message.answer("⏳ Формирую документ со всеми билетами, подождите...")
    
    async with async_session() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.status == "Active").order_by(Ticket.user_id)
        )
        tickets = result.scalars().all()
    
    if not tickets:
        await callback.message.answer("В базе пока нет активных билетов.", reply_markup=admin_back_keyboard())
        return await callback.answer()
        
    filename = "active_tickets.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"ВСЕГО АКТИВНЫХ БИЛЕТОВ: {len(tickets)}\n")
        f.write("="*40 + "\n")
        for t in tickets:
            date_str = t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else 'N/A'
            f.write(f"Билет: {t.ticket_number} | ID Пользователя: {t.user_id} | Тип: {t.ticket_type} | Выдан: {date_str}\n")
            
    document = FSInputFile(filename)
    await callback.message.answer_document(
        document=document,
        caption="🗂 <b>Список всех активных билетов</b>\nВы можете открыть этот файл на компьютере или телефоне.",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML"
    )
    
    if os.path.exists(filename):
        os.remove(filename)
        
    await callback.answer()

@router.callback_query(F.data == "admin_period_stats")
async def admin_period_stats_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data="stats_today"),
            InlineKeyboardButton(text="Неделя", callback_data="stats_week")
        ],
        [
            InlineKeyboardButton(text="Месяц", callback_data="stats_month"),
            InlineKeyboardButton(text="Всё время", callback_data="stats_all")
        ],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    text = (
        "📅 <b>Билеты по датам</b>\n"
        "───────────────────\n"
        "Выберите интересующий период, чтобы узнать количество сгенерированных билетов:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("stats_"))
async def calc_stats(callback: CallbackQuery):
    period = callback.data.split("_")[1]
    now = datetime.now()
    
    async with async_session() as session:
        if period == "today":
            start_dt = datetime.combine(now.date(), time.min)
            query = select(func.count()).select_from(Ticket).where(Ticket.created_at >= start_dt)
        elif period == "week":
            start_dt = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min)
            query = select(func.count()).select_from(Ticket).where(Ticket.created_at >= start_dt)
        elif period == "month":
            start_dt = datetime.combine(now.date().replace(day=1), time.min)
            query = select(func.count()).select_from(Ticket).where(Ticket.created_at >= start_dt)
        elif period == "all":
            query = select(func.count()).select_from(Ticket)
        else:
            return await callback.answer()

        count = await session.scalar(query)
        
    period_names = {"today": "сегодня", "week": "эту неделю", "month": "этот месяц", "all": "всё время"}
    text = (
        "📅 <b>Билеты за период</b>\n"
        "───────────────────\n"
        f"🎫 Всего сгенерировано за <b>{period_names[period]}</b>:\n"
        f"👉 <b>{count} шт.</b>\n"
        "───────────────────"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_pending_reqs")
async def show_pending_reqs(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
    await show_requests_list(callback.message.chat.id, callback, page=1)
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем", callback_data="broad_target_all")],
        [InlineKeyboardButton(text="🟢 Активным подпискам", callback_data="broad_target_active")],
        [InlineKeyboardButton(text="🔴 Неактивным подпискам", callback_data="broad_target_inactive")],
        [InlineKeyboardButton(text="🎟 Без билета", callback_data="broad_target_no_tickets")],
        [InlineKeyboardButton(text="📅 По дате регистрации", callback_data="broad_target_join_date")],
        [InlineKeyboardButton(text="🏆 По тарифу", callback_data="broad_target_tier_menu")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    text = (
        "📢 <b>Рассылка сообщений</b>\n"
        "───────────────────\n"
        "Выберите целевую аудиторию для рассылки:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "broad_target_tier_menu")
async def broad_target_tier_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Уровень 2 (Мини)", callback_data="broad_target_tier_2")],
        [InlineKeyboardButton(text="Уровень 3 (Стандарт)", callback_data="broad_target_tier_3")],
        [InlineKeyboardButton(text="Уровень 4 (ВИП)", callback_data="broad_target_tier_4")],
        [InlineKeyboardButton(text="Уровень 5 (ПРЕМИУМ)", callback_data="broad_target_tier_5")],
        [InlineKeyboardButton(text="« Назад к рассылке", callback_data="admin_broadcast")]
    ])
    
    text = "Выберите уровень доступа для рассылки:"
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("broad_target_"))
async def process_broad_target_selection(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
        
    target = callback.data.split("broad_target_")[1]
    
    if target == "join_date":
        await callback.message.edit_text(
            "Введите дату регистрации пользователей в формате <code>ГГГГ-ММ-ДД</code> (например, <code>2026-06-21</code>):",
            reply_markup=admin_back_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_broadcast_join_date)
        await callback.answer()
        return
        
    await state.update_data(broadcast_target=target)
    
    target_names = {
        "all": "всем пользователям",
        "active": "активным подпискам",
        "inactive": "неактивным подпискам",
        "no_tickets": "пользователям без активных билетов",
        "tier_2": "пользователям с тарифом 2",
        "tier_3": "пользователям с тарифом 3",
        "tier_4": "пользователям с тарифом 4",
        "tier_5": "пользователям с тарифом 5",
    }
    
    target_name = target_names.get(target, "выбранной аудитории")
    await callback.message.edit_text(
        f"Отправьте сообщение для рассылки ({target_name}). Можно прикрепить фото или видео:",
        reply_markup=admin_back_keyboard()
    )
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast_join_date)
async def process_broadcast_join_date(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return await message.answer(
            "❌ Неверный формат даты. Введите дату в формате: <code>ГГГГ-ММ-ДД</code> (например, <code>2026-06-21</code>):",
            reply_markup=admin_back_keyboard(),
            parse_mode="HTML"
        )
        
    await state.update_data(broadcast_target="join_date", broadcast_target_value=date_str)
    await message.answer(
        f"Отправьте сообщение для рассылки пользователям, зарегистрировавшимся {date_str}. Можно прикрепить фото или видео:",
        reply_markup=admin_back_keyboard()
    )
    await state.set_state(AdminStates.waiting_for_broadcast_message)

@router.message(AdminStates.waiting_for_broadcast_message)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot, album: list[Message] = None):
    if not is_admin(message.from_user.id):
        return
        
    data = await state.get_data()
    target = data.get("broadcast_target", "all")
    target_val = data.get("broadcast_target_value")
    
    async with async_session() as session:
        if target == "all":
            query = select(User.telegram_id)
        elif target == "active":
            query = select(User.telegram_id).where(User.subscription_status == "Active")
        elif target == "inactive":
            query = select(User.telegram_id).where(
                (User.subscription_status == "Inactive") | 
                (User.subscription_status == "Expired") | 
                (User.subscription_status.is_(None))
            )
        elif target == "no_tickets":
            from sqlalchemy import exists
            query = select(User.telegram_id).where(
                ~exists().where((Ticket.user_id == User.telegram_id) & (Ticket.status == "Active"))
            )
        elif target == "join_date":
            query = select(User.telegram_id).where(func.date(User.created_at) == target_val)
        elif target.startswith("tier_"):
            try:
                tier_num = int(target.split("_")[1])
            except (ValueError, IndexError):
                tier_num = 0
            query = select(User.telegram_id).where(User.tier == tier_num)
        else:
            query = select(User.telegram_id)
            
        users_res = await session.execute(query)
        users = users_res.scalars().all()

    sent = 0
    msg = await message.answer(f"⏳ Начинаю рассылку для {len(users)} пользователей...")
    
    if album:
        media_group = []
        for idx, item in enumerate(album):
            cap = item.caption if idx == 0 else None
            cap_entities = item.caption_entities if idx == 0 else None
            if item.photo:
                media_group.append(InputMediaPhoto(media=item.photo[-1].file_id, caption=cap, caption_entities=cap_entities))
            elif item.video:
                media_group.append(InputMediaVideo(media=item.video.file_id, caption=cap, caption_entities=cap_entities))

        for uid in users:
            try:
                await bot.send_media_group(chat_id=uid, media=media_group)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
    else:
        for uid in users:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
            
    await msg.edit_text(f"✅ Рассылка завершена!\nУспешно доставлено: {sent} из {len(users)}", reply_markup=admin_back_keyboard())
    await state.clear()

@router.callback_query(F.data == "admin_stats")
async def show_stats(callback: CallbackQuery):
    async with async_session() as session:
        total_users = await session.scalar(select(func.count()).select_from(User))
        active_subs = await session.scalar(select(func.count()).select_from(User).where(User.subscription_status == "Active"))
        active_tickets = await session.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
        total_tickets = await session.scalar(select(func.count()).select_from(Ticket))
        
        # Загружаем лимит билетов
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None

    if limit_val is not None:
        percent = (active_tickets / limit_val) * 100 if limit_val > 0 else 0
        filled_chars = min(int(percent / 10), 10)
        
        if percent < 50:
            char = "🟩"
        elif percent < 85:
            char = "🟨"
        else:
            char = "🟥"
            
        bar = char * filled_chars + "⬜" * (10 - filled_chars)
        active_tickets_str = f"<b>{active_tickets}</b> из <b>{limit_val}</b> ({percent:.1f}%)\n└ {bar}"
    else:
        active_tickets_str = f"<b>{active_tickets}</b> (Без лимита)"

    text = (
        "📊 <b>Статистика проекта</b>\n"
        "───────────────────\n"
        f"👥 Всего пользователей в боте: <b>{total_users}</b>\n"
        f"⭐️ Активных подписок: <b>{active_subs}</b>\n"
        "───────────────────\n"
        f"🎟 Активных билетов в розыгрыше: {active_tickets_str}\n"
        f"🎫 Всего сгенерировано билетов: <b>{total_tickets}</b>\n"
        "───────────────────"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_search_user")
async def start_user_search(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text("Введите Telegram ID или Номер телефона пользователя (например, +79991234567):", reply_markup=admin_back_keyboard())
    except Exception:
        await callback.message.delete()
        await callback.message.answer("Введите Telegram ID или Номер телефона пользователя (например, +79991234567):", reply_markup=admin_back_keyboard())
    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()

@router.message(AdminStates.waiting_for_user_search)
async def process_user_search(message: Message, state: FSMContext):
    search_val = message.text.strip()
    is_phone = False
    
    if search_val.startswith('+') or (search_val.isdigit() and len(search_val) >= 10):
        is_phone = True
        
    clean_phone = ''.join(c for c in search_val if c.isdigit())
    if len(clean_phone) == 11 and clean_phone[0] in ('7', '8'):
        clean_phone = clean_phone[1:]
    
    async with async_session() as session:
        if is_phone:
            user_res = await session.execute(select(User).where(User.phone_number.like(f"%{clean_phone}%")))
            users = user_res.scalars().all()
        else:
            try:
                user_id = int(search_val)
                user_res = await session.execute(select(User).where(User.telegram_id == user_id))
                users = user_res.scalars().all()
            except ValueError:
                user_res = await session.execute(select(User).where(User.phone_number.like(f"%{clean_phone}%")))
                users = user_res.scalars().all()

    if not users:
        return await message.answer("Пользователь не найден в базе по данному ID или номеру телефона.", reply_markup=admin_back_keyboard())
        
    if len(users) > 1:
        buttons = []
        for u in users:
            phone_str = f" | {u.phone_number}" if u.phone_number else ""
            buttons.append([InlineKeyboardButton(text=f"ID: {u.telegram_id}{phone_str}", callback_data=f"usr_view_{u.telegram_id}")])
        buttons.append([InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("Найдено несколько пользователей. Выберите нужного:", reply_markup=markup)
        await state.clear()
        return
        
    user = users[0]
    await show_user_profile_card(message, user.telegram_id, state)

async def show_user_profile_card(message: Message, telegram_id: int, state: FSMContext, edit_message=False):
    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_res.scalars().first()
        if not user:
            return
        tickets_res = await session.execute(select(Ticket).where(Ticket.user_id == telegram_id, Ticket.status == "Active"))
        tickets = tickets_res.scalars().all()
        
    ticket_list = "\n".join([f"<code>{t.ticket_number}</code> ({t.ticket_type})" for t in tickets]) if tickets else "Нет"
    ban_status = "🚫 Заблокирован" if user.is_banned else "🟢 Активен"
    
    text = (
        f"👤 <b>Профиль пользователя</b> <code>{user.telegram_id}</code>:\n"
        f"───────────────────\n"
        f"Статус подписки: <b>{user.subscription_status}</b>\n"
        f"Уровень: <b>{user.tier}</b>\n"
        f"Истекает: <b>{user.expire_date}</b>\n"
        f"Телефон: <b>{user.phone_number or 'Не указан'}</b>\n"
        f"Статус бана: <b>{ban_status}</b>\n\n"
        f"🎟 Активные билеты ({len(tickets)}):\n{ticket_list}\n"
        f"───────────────────"
    )
    
    ban_btn_text = "🟢 Разбанить" if user.is_banned else "🚫 Забанить"
    ban_callback = f"usr_unban_{user.telegram_id}" if user.is_banned else f"usr_ban_{user.telegram_id}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=ban_btn_text, callback_data=ban_callback),
            InlineKeyboardButton(text="📅 Срок подписки", callback_data=f"usr_sub_edit_{user.telegram_id}")
        ],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("usr_view_"))
async def usr_view_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("usr_view_")[1])
    await show_user_profile_card(callback.message, user_id, state, edit_message=True)
    await callback.answer()

@router.callback_query(F.data.startswith("usr_ban_") | F.data.startswith("usr_unban_"))
async def usr_ban_unban_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[-1])
    is_ban = "usr_ban_" in callback.data
    
    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = user_res.scalars().first()
        if user:
            user.is_banned = is_ban
            await session.commit()
            
    await callback.answer("Пользователь заблокирован." if is_ban else "Пользователь разблокирован.", show_alert=True)
    await show_user_profile_card(callback.message, user_id, state, edit_message=True)

@router.callback_query(F.data.startswith("usr_sub_edit_"))
async def usr_sub_edit_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("usr_sub_edit_")[1])
    await state.update_data(edit_sub_user_id=user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 1 месяц", callback_data=f"usr_sub_add_1m_{user_id}"),
            InlineKeyboardButton(text="➕ 3 месяца", callback_data=f"usr_sub_add_3m_{user_id}")
        ],
        [InlineKeyboardButton(text="❌ Сбросить подписку", callback_data=f"usr_sub_reset_{user_id}")],
        [InlineKeyboardButton(text="« Отмена", callback_data=f"usr_view_{user_id}")]
    ])
    
    text = (
        f"📅 <b>Изменение срока подписки для пользователя</b> <code>{user_id}</code>:\n"
        f"───────────────────\n"
        "Отправьте новую дату окончания подписки в формате <code>ГГГГ-ММ-ДД</code> (например, <code>2026-07-01</code>) "
        "или выберите один из быстрых вариантов на кнопках ниже:"
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_user_sub_date)
    await callback.answer()

@router.callback_query(F.data.startswith("usr_sub_add_") | F.data.startswith("usr_sub_reset_"))
async def usr_sub_quick_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[-1])
    action = callback.data
    
    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = user_res.scalars().first()
        if user:
            if "reset" in action:
                user.subscription_status = "Inactive"
                user.expire_date = None
            else:
                months = 3 if "3m" in action else 1
                current_expire = user.expire_date or date.today()
                days_in_month = 30 * months
                user.expire_date = current_expire + timedelta(days=days_in_month)
                user.subscription_status = "Active"
            await session.commit()
            asyncio.create_task(sync_user_data(user_id))
            
    await callback.answer("Подписка обновлена.", show_alert=True)
    await show_user_profile_card(callback.message, user_id, state, edit_message=True)

@router.message(AdminStates.waiting_for_user_sub_date)
async def process_user_sub_date_text(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("edit_sub_user_id")
    if not user_id:
        await state.clear()
        return
        
    text = message.text.strip()
    try:
        new_date = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return await message.answer("❌ Неверный формат. Пожалуйста, введите дату в формате: <code>ГГГГ-ММ-ДД</code> (например, <code>2026-07-01</code>):", parse_mode="HTML")
        
    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = user_res.scalars().first()
        if user:
            user.expire_date = new_date
            user.subscription_status = "Active" if new_date >= date.today() else "Expired"
            await session.commit()
            asyncio.create_task(sync_user_data(user_id))
            
    await message.answer(f"✅ Подписка для пользователя <code>{user_id}</code> продлена до {new_date}.", parse_mode="HTML")
    await show_user_profile_card(message, user_id, state)

@router.callback_query(F.data == "admin_give_tickets")
async def start_manual_ticket(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text("Кому выдаем билеты? Введите Telegram ID пользователя:", reply_markup=admin_back_keyboard())
    except Exception:
        await callback.message.delete()
        await callback.message.answer("Кому выдаем билеты? Введите Telegram ID пользователя:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminStates.waiting_for_manual_ticket_user)
    await callback.answer()

@router.message(AdminStates.waiting_for_manual_ticket_user)
async def process_manual_ticket_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(ticket_target_id=user_id)
        await message.answer("Сколько билетов выдать? Введите число:", reply_markup=admin_back_keyboard())
        await state.set_state(AdminStates.waiting_for_manual_ticket_count)
    except ValueError:
        await message.answer("Неверный ID. Попробуйте еще раз.", reply_markup=admin_back_keyboard())

@router.message(AdminStates.waiting_for_manual_ticket_count)
async def process_manual_ticket_count(message: Message, state: FSMContext, bot: Bot):
    try:
        count = int(message.text)
        data = await state.get_data()
        user_id = data['ticket_target_id']
    except ValueError:
        return await message.answer("Нужно ввести число.", reply_markup=admin_back_keyboard())

    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == user_id))
        if not user_res.scalars().first():
            return await message.answer("Такого пользователя нет в базе.", reply_markup=admin_back_keyboard())

        await generate_tickets(session, user_id, count, "bonus")
        await session.commit()
        asyncio.create_task(sync_user_data(user_id))

    await message.answer(f"✅ Успешно выдано {count} билетов пользователю <code>{user_id}</code>!", reply_markup=admin_back_keyboard(), parse_mode="HTML")
    try:
        await bot.send_message(user_id, f"🎁 Администратор выдал вам {count} бонусных билетов!")
    except Exception:
        pass
    await state.clear()

@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def process_subscription_request(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return

    action, req_id = callback.data.split("_")
    req_id = int(req_id)

    async with async_session() as session:
        result = await session.execute(select(SubscriptionRequest).where(SubscriptionRequest.id == req_id))
        req = result.scalars().first()

        if not req or req.status != "Pending":
            await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            return

        user_result = await session.execute(select(User).where(User.telegram_id == req.telegram_id))
        user = user_result.scalars().first()

        if action == "approve":
            tickets_to_generate = await get_tier_tickets(session, req.tier)

            # Проверяем лимит билетов
            limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
            limit_cfg = limit_res.scalars().first()
            limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None
            
            if limit_val is not None and tickets_to_generate > 0:
                tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
                total_active = tickets_count_res.scalar() or 0
                if total_active + tickets_to_generate > limit_val:
                    limit_left = max(0, limit_val - total_active)
                    await callback.answer(
                        f"🚫 Ошибка: превышен лимит билетов!\n"
                        f"Осталось свободных билетов: {limit_left} шт.\n"
                        f"Данный тариф требует: {tickets_to_generate} шт.\n"
                        f"Сначала увеличьте лимит билетов в настройках.",
                        show_alert=True
                    )
                    return

            req.status = "Approved"
            user.subscription_status = "Active"
            user.tier = req.tier
            user.expire_date = None

            if tickets_to_generate > 0:
                await generate_tickets(session, user.telegram_id, tickets_to_generate, "regular")

            if user.ref_id and user.ref_id != user.telegram_id and not user.referral_rewarded:
                referrer_id = user.ref_id
                user.referral_rewarded = True
                
                ref_result = await session.execute(select(User).where(User.telegram_id == referrer_id))
                referrer = ref_result.scalars().first()
                if referrer:
                    await generate_tickets(session, referrer.telegram_id, 1, "bonus")
                    try:
                        await bot.send_message(referrer.telegram_id, "Вам начислен 1 бонусный купон за приглашенного друга!")
                    except Exception as e:
                        logging.error(f"Failed to notify referrer: {e}")
                    asyncio.create_task(sync_user_data(referrer_id))

            # КРИТИЧНО: commit ДО отправки сообщений, чтобы данные сохранились
            # даже если последующие Telegram-запросы завершатся с ошибкой
            await session.commit()
            asyncio.create_task(sync_user_data(req.telegram_id))

            try:
                if PRIVATE_CHANNEL_ID != 0:
                    invite = await bot.create_chat_invite_link(
                        chat_id=PRIVATE_CHANNEL_ID,
                        member_limit=1,
                        name=f"Sub_{user.telegram_id}"
                    )
                    invite_link = invite.invite_link
                else:
                    invite_link = os.getenv("PRIVATE_CHANNEL_LINK", "Ссылка не настроена")

                text = (
                    "Поздравляем! Ваша оплата прошла!\n"
                    f"Вам начислено купонов: {tickets_to_generate} шт\n\n"
                    f"🔑 Ваша персональная ссылка на канал розыгрыша:\n{invite_link}\n\n"
                    "Подписывайтесь, чтобы не пропустить итоги розыгрыша."
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Подписаться на канал", url=invite_link)],
                        [InlineKeyboardButton(text="Поддержка", callback_data="menu_support")],
                        [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
                    ]
                )

                coupon_photo = "kupon.jpg"
                if os.path.exists(coupon_photo):
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=FSInputFile(coupon_photo),
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Failed to create invite link or notify user: {e}")
            
            # Редактируем сообщение с кнопками — ошибка не критична, данные уже сохранены
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ ОДОБРЕНО", reply_markup=None)
                else:
                    await callback.message.edit_text(text=callback.message.text + "\n\n✅ ОДОБРЕНО", reply_markup=None)
            except Exception as e:
                logging.error(f"Could not edit approval message (non-critical): {e}")

        elif action == "reject":
            req.status = "Rejected"
            try:
                await bot.send_message(user.telegram_id, "Ваша заявка на подписку была отклонена администратором.")
            except Exception as e:
                logging.error(f"Failed to notify user about rejection: {e}")

            # КРИТИЧНО: commit ДО редактирования сообщения
            await session.commit()
            asyncio.create_task(sync_user_data(req.telegram_id))

            # Редактируем сообщение с кнопками — ошибка не критична, данные уже сохранены
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)
                else:
                    await callback.message.edit_text(text=callback.message.text + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)
            except Exception as e:
                logging.error(f"Could not edit rejection message (non-critical): {e}")

    await callback.answer()

@router.callback_query(F.data == "edit_payment_config")
async def edit_payment_config_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Инструкции тарифов", callback_data="edit_payment_tiers")],
        [InlineKeyboardButton(text="🖼 Загрузить QR-код оплаты", callback_data="edit_payment_global_qr")],
        [InlineKeyboardButton(text="🔗 Настроить ссылку СБП", callback_data="edit_payment_sbp_link")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    text = (
        "⚙️ <b>Настройка оплаты</b>\n"
        "───────────────────\n"
        "Выберите действие:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "edit_payment_sbp_link")
async def edit_payment_sbp_link_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    
    async with async_session() as session:
        sbp_res = await session.execute(select(BotConfig).where(BotConfig.key == "sbp_static_link"))
        sbp_cfg = sbp_res.scalars().first()
        current_link = sbp_cfg.value if sbp_cfg and sbp_cfg.value else "Не настроена"

    text = (
        "🔗 <b>Настройка статической ссылки СБП</b>\n\n"
        f"Текущая ссылка: <code>{current_link}</code>\n\n"
        "Отправьте новую статическую СБП-ссылку от вашего банка (например: <code>https://qr.nspk.ru/AD1000...</code>) "
        "или отправьте слово 'удалить' для сброса настройки:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
        
    await state.set_state(AdminStates.waiting_for_sbp_link)
    await callback.answer()

@router.message(AdminStates.waiting_for_sbp_link)
async def process_sbp_link_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    input_text = message.text.strip()
    
    async with async_session() as session:
        sbp_res = await session.execute(select(BotConfig).where(BotConfig.key == "sbp_static_link"))
        sbp_cfg = sbp_res.scalars().first()
        
        if input_text.lower() == 'удалить':
            if sbp_cfg:
                await session.delete(sbp_cfg)
                await session.commit()
            await message.answer("✅ Ссылка СБП удалена. Будет использоваться глобальный статический QR-код.", reply_markup=admin_back_keyboard())
        else:
            if not input_text.startswith("http://") and not input_text.startswith("https://") and not input_text.startswith("sbp://"):
                await message.answer("⚠️ Некорректный формат ссылки! Ссылка должна начинаться с https:// или qr.nspk.ru. Попробуйте еще раз:", reply_markup=admin_back_keyboard())
                return
            
            if sbp_cfg:
                sbp_cfg.value = input_text
            else:
                sbp_cfg = BotConfig(key="sbp_static_link", value=input_text)
                session.add(sbp_cfg)
            await session.commit()
            await message.answer(f"✅ Ссылка СБП успешно сохранена:\n<code>{input_text}</code>", reply_markup=admin_back_keyboard(), parse_mode="HTML")
            
    await state.clear()

@router.callback_query(F.data == "edit_payment_tiers")
async def edit_payment_config_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    try:
        await callback.message.edit_text("Введите номер уровня для редактирования (2, 3, 4 или 5):", reply_markup=admin_back_keyboard())
    except Exception:
        await callback.message.delete()
        await callback.message.answer("Введите номер уровня для редактирования (2, 3, 4 или 5):", reply_markup=admin_back_keyboard())
    
    await state.set_state(AdminStates.waiting_for_payment_config_tier)
    await callback.answer()

@router.callback_query(F.data == "edit_payment_global_qr")
async def edit_payment_global_qr_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    try:
        await callback.message.edit_text("Пришлите фото QR-кода для оплаты:", reply_markup=admin_back_keyboard())
    except Exception:
        await callback.message.delete()
        await callback.message.answer("Пришлите фото QR-кода для оплаты:", reply_markup=admin_back_keyboard())
    
    await state.set_state(AdminStates.waiting_for_global_qr)
    await callback.answer()

@router.message(AdminStates.waiting_for_global_qr, F.photo)
async def process_global_qr_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    photo_id = message.photo[-1].file_id
    
    async with async_session() as session:
        result = await session.execute(select(BotConfig).where(BotConfig.key == "global_payment_qr"))
        config_obj = result.scalars().first()
        
        if config_obj:
            config_obj.value = photo_id
        else:
            config_obj = BotConfig(key="global_payment_qr", value=photo_id)
            session.add(config_obj)
            
        await session.commit()
        
    await message.answer("✅ Глобальный QR-код оплаты успешно сохранен!", reply_markup=admin_back_keyboard())
    await state.clear()

@router.message(AdminStates.waiting_for_global_qr)
async def process_global_qr_photo_fallback(message: Message):
    await message.answer("Пожалуйста, пришлите именно фотографию (изображение) QR-кода оплаты.", reply_markup=admin_back_keyboard())

@router.message(AdminStates.waiting_for_payment_config_tier)
async def get_config_tier(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        tier = int(message.text)
        if tier not in [2, 3, 4, 5]:
            raise ValueError
    except ValueError:
        return await message.answer("Неверный уровень. Введите 2, 3, 4 или 5:", reply_markup=admin_back_keyboard())
    
    await state.update_data(edit_tier=tier)
    await message.answer("Введите новый текст инструкции об оплате:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminStates.waiting_for_payment_config_text)

@router.message(AdminStates.waiting_for_payment_config_text)
async def get_config_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(edit_text=message.text)
    await message.answer("Пришлите фото инструкции или отправьте 'нет' для сохранения без фото:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminStates.waiting_for_payment_config_photo)

@router.message(AdminStates.waiting_for_payment_config_photo)
async def get_config_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tier = data['edit_tier']
    text = data['edit_text']
    photo_id = message.photo[-1].file_id if message.photo else None

    async with async_session() as session:
        result = await session.execute(select(PaymentConfig).where(PaymentConfig.tier == tier))
        config_obj = result.scalars().first()

        if config_obj:
            config_obj.text = text
            config_obj.photo_file_id = photo_id
        else:
            config_obj = PaymentConfig(tier=tier, text=text, photo_file_id=photo_id)
            session.add(config_obj)
        
        await session.commit()
    
    await message.answer(f"Инструкция для уровня {tier} успешно обновлена!", reply_markup=admin_back_keyboard())
    await state.clear()


# --- ПЛАНИРОВЩИК ПОСТОВ (АВТОПОСТИНГ) ---

@router.callback_query(F.data == "admin_scheduler_menu")
async def admin_scheduler_menu_cmd(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать пост", callback_data="admin_sched_create")],
        [
            InlineKeyboardButton(text="📋 Запланированные", callback_data="admin_sched_list"),
            InlineKeyboardButton(text="📜 История постов", callback_data="admin_sched_history")
        ],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    text = (
        "📅 <b>Планировщик постов</b>\n"
        "───────────────────\n"
        "Управление автопостингом и отложенными публикациями в закрытый канал.\n\n"
        "• <b>Шаг 1:</b> Нажмите «➕ Создать пост»\n"
        "• <b>Шаг 2:</b> Отправьте медиа или текст\n"
        "• <b>Шаг 3:</b> Пришлите кнопки и укажите время публикации\n"
        "───────────────────"
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_sched_create")
async def admin_sched_create_cmd(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_scheduler_menu")]
    ])
    
    await callback.message.edit_text(
        "📝 <b>Шаг 1: Отправьте контент для поста.</b>\n\n"
        "Это может быть обычный текст, изображение с подписью или видео с подписью.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_post_content)
    await callback.answer()

@router.message(AdminStates.waiting_for_post_content)
async def process_post_content(message: Message, state: FSMContext, album: list[Message] = None):
    if not is_admin(message.from_user.id):
        return

    text = message.text or message.caption or ""
    media_file_id = None
    media_type = "none"

    if album:
        media_items = []
        for msg in album:
            if msg.photo:
                media_items.append({"type": "photo", "file_id": msg.photo[-1].file_id})
            elif msg.video:
                media_items.append({"type": "video", "file_id": msg.video.file_id})
        if media_items:
            media_file_id = json.dumps(media_items)
            media_type = "album"
            for msg in album:
                if msg.caption:
                    text = msg.caption
                    break
    else:
        if message.photo:
            media_file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.video:
            media_file_id = message.video.file_id
            media_type = "video"

    await state.update_data(
        post_text=text,
        post_media_file_id=media_file_id,
        post_media_type=media_type
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip_post_buttons")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_scheduler_menu")]
    ])

    await message.answer(
        "🔗 <b>Шаг 2: Добавьте кнопки-ссылки (опционально).</b>\n\n"
        "Если вы хотите добавить инлайн-кнопки под постом, отправьте текст и ссылку в формате:\n"
        "<code>Название кнопки | https://ссылка</code>\n\n"
        "Если кнопок несколько, укажите каждую с новой строки.\n"
        "Если кнопки не нужны, нажмите кнопку «Пропустить» ниже или отправьте слово <b>нет</b>.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_post_buttons)

@router.callback_query(AdminStates.waiting_for_post_buttons, F.data == "skip_post_buttons")
async def skip_post_buttons_cmd(callback: CallbackQuery, state: FSMContext):
    await state.update_data(post_buttons_json=None)
    await ask_for_post_time(callback.message, state)
    await callback.answer()

@router.message(AdminStates.waiting_for_post_buttons)
async def process_post_buttons(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip() if message.text else ""
    if text.lower() == "нет":
        await state.update_data(post_buttons_json=None)
        await ask_for_post_time(message, state)
        return

    # Парсим кнопки
    lines = text.split("\n")
    buttons = []
    for line in lines:
        if "|" in line:
            parts = line.split("|", 1)
            btn_text = parts[0].strip()
            btn_url = parts[1].strip()
            if btn_text and btn_url.startswith("http"):
                buttons.append({"text": btn_text, "url": btn_url})
            else:
                return await message.answer("❌ Неверный формат ссылки. Убедитесь, что ссылка начинается с http/https. Попробуйте еще раз или напишите 'нет'.")
        else:
            return await message.answer("❌ Неверный формат. Используйте разделитель '|' (например: Название | https://ссылка). Попробуйте еще раз или напишите 'нет'.")

    await state.update_data(post_buttons_json=json.dumps(buttons))
    await ask_for_post_time(message, state)

async def ask_for_post_time(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="sched_time_now")],
        [InlineKeyboardButton(text="⏰ +1 час", callback_data="sched_time_1h")],
        [InlineKeyboardButton(text="⏰ +3 часа", callback_data="sched_time_3h")],
        [InlineKeyboardButton(text="🌙 Сегодня в 20:00", callback_data="sched_time_today_20")],
        [InlineKeyboardButton(text="☀️ Завтра в 10:00", callback_data="sched_time_tomorrow_10")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_scheduler_menu")]
    ])

    await message.answer(
        "📅 <b>Шаг 3: Выберите время публикации.</b>\n\n"
        "Введите дату и время вручную в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (например, <code>2026-06-05 15:30</code>)\n"
        "Или выберите один из быстрых вариантов ниже:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_post_time)

@router.message(AdminStates.waiting_for_post_time)
async def process_post_time_text(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    try:
        publish_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
        if publish_at < datetime.now():
            return await message.answer("❌ Время публикации не может быть в прошлом! Введите будущее время:")
    except ValueError:
        return await message.answer("❌ Неверный формат даты и времени. Используйте формат: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (например, <code>2026-06-05 15:30</code>)")

    data = await state.get_data()
    await create_scheduled_post(data, publish_at, bot)
    await state.clear()
    await message.answer(f"✅ Пост успешно запланирован на {publish_at.strftime('%Y-%m-%d %H:%M')}", reply_markup=admin_back_keyboard())

@router.callback_query(AdminStates.waiting_for_post_time, F.data.startswith("sched_time_"))
async def process_post_time_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        return

    action = callback.data.split("sched_time_")[1]
    now = datetime.now()
    publish_at = now
    immediate = False

    if action == "now":
        immediate = True
    elif action == "1h":
        publish_at = now + timedelta(hours=1)
    elif action == "3h":
        publish_at = now + timedelta(hours=3)
    elif action == "today_20":
        publish_at = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if publish_at < now:
            publish_at += timedelta(days=1)
    elif action == "tomorrow_10":
        publish_at = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)

    data = await state.get_data()
    post_id = await create_scheduled_post(data, publish_at, bot, immediate=immediate)
    await state.clear()

    try:
        await callback.message.delete()
    except Exception:
        pass

    if immediate:
        await callback.message.answer("🚀 Пост опубликован в канале!", reply_markup=admin_back_keyboard())
    else:
        await callback.message.answer(f"✅ Пост успешно запланирован на {publish_at.strftime('%Y-%m-%d %H:%M')}", reply_markup=admin_back_keyboard())
    await callback.answer()

async def create_scheduled_post(data, publish_at, bot: Bot, immediate=False) -> int:
    from services.scheduler import publish_post_to_channel

    async with async_session() as session:
        post = ScheduledPost(
            text=data.get("post_text"),
            media_file_id=data.get("post_media_file_id"),
            media_type=data.get("post_media_type"),
            buttons_json=data.get("post_buttons_json"),
            publish_at=publish_at,
            status="Pending"
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        
        post_id = post.id

        if immediate:
            success = await publish_post_to_channel(bot, post)
            post.status = "Sent" if success else "Failed"
            await session.commit()

    return post_id

@router.callback_query(F.data == "admin_sched_list")
async def admin_sched_list_cmd(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.status == "Pending").order_by(ScheduledPost.publish_at)
        )
        posts = result.scalars().all()

    if not posts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать пост", callback_data="admin_sched_create")],
            [InlineKeyboardButton(text="« Назад", callback_data="admin_scheduler_menu")]
        ])
        await callback.message.edit_text("📋 Нет запланированных постов.", reply_markup=keyboard)
        await callback.answer()
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer("📋 <b>Список запланированных постов:</b>", parse_mode="HTML")
    for post in posts:
        preview = post.text[:100] + "..." if post.text and len(post.text) > 100 else (post.text or "[Без текста]")
        media_info = f"Фото" if post.media_type == "photo" else (f"Видео" if post.media_type == "video" else (f"Альбом" if post.media_type == "album" else "Текст"))
        
        text = (
            f"<b>Пост #{post.id}</b> ({media_info})\n"
            f"📅 Время публикации: {post.publish_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"📝 Текст: {preview}"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"sched_pub_now_{post.id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sched_del_{post.id}")
            ]
        ])
        
        if post.media_type == "photo":
            await callback.message.answer_photo(photo=post.media_file_id, caption=text, reply_markup=keyboard, parse_mode="HTML")
        elif post.media_type == "video":
            await callback.message.answer_video(video=post.media_file_id, caption=text, reply_markup=keyboard, parse_mode="HTML")
        elif post.media_type == "album":
            try:
                media_items = json.loads(post.media_file_id)
                media_group = []
                for idx, item in enumerate(media_items):
                    file_id = item.get("file_id")
                    m_type = item.get("type", "photo")
                    cap = text if idx == 0 else None
                    parse_m = "HTML" if idx == 0 else None
                    if m_type == "video":
                        media_group.append(InputMediaVideo(media=file_id, caption=cap, parse_mode=parse_m))
                    else:
                        media_group.append(InputMediaPhoto(media=file_id, caption=cap, parse_mode=parse_m))
                await callback.message.answer_media_group(media=media_group)
                await callback.message.answer(f"Управление постом #{post.id}:", reply_markup=keyboard)
            except Exception as e:
                logging.error(f"Error displaying scheduled album: {e}")
                await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    await callback.message.answer("Панель планировщика:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в меню планировщика", callback_data="admin_scheduler_menu")]
    ]))
    await callback.answer()

@router.callback_query(F.data.startswith("sched_pub_now_"))
async def sched_pub_now_callback(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return

    post_id = int(callback.data.split("sched_pub_now_")[1])
    from services.scheduler import publish_post_to_channel

    async with async_session() as session:
        result = await session.execute(select(ScheduledPost).where(ScheduledPost.id == post_id))
        post = result.scalars().first()

        if post and post.status == "Pending":
            success = await publish_post_to_channel(bot, post)
            post.status = "Sent" if success else "Failed"
            await session.commit()
            
            if success:
                await callback.message.answer(f"🚀 Пост #{post_id} успешно опубликован в канале!")
                try:
                    await callback.message.delete()
                except Exception:
                    pass
            else:
                await callback.message.answer(f"❌ Ошибка публикации поста #{post_id}.")
        else:
            await callback.answer("Пост не найден или уже опубликован.", show_alert=True)

    await callback.answer()

@router.callback_query(F.data.startswith("sched_del_"))
async def sched_del_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    post_id = int(callback.data.split("sched_del_")[1])
    async with async_session() as session:
        result = await session.execute(select(ScheduledPost).where(ScheduledPost.id == post_id))
        post = result.scalars().first()

        if post:
            await session.delete(post)
            await session.commit()
            await callback.message.answer(f"🗑 Запланированный пост #{post_id} успешно удален.")
            try:
                await callback.message.delete()
            except Exception:
                pass
        else:
            await callback.answer("Пост не найден.", show_alert=True)

    await callback.answer()

@router.callback_query(F.data == "admin_sched_history")
async def admin_sched_history_cmd(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.status != "Pending").order_by(ScheduledPost.publish_at.desc()).limit(10)
        )
        posts = result.scalars().all()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад", callback_data="admin_scheduler_menu")]
    ])

    if not posts:
        await callback.message.edit_text("📜 История публикаций пуста.", reply_markup=keyboard)
        await callback.answer()
        return

    text = "📜 <b>История последних 10 постов:</b>\n\n"
    for post in posts:
        preview = post.text[:50] + "..." if post.text and len(post.text) > 50 else (post.text or "[Без текста]")
        status_icon = "✅ Отправлен" if post.status == "Sent" else "❌ Ошибка"
        text += (
            f"• <b>Пост #{post.id}</b>\n"
            f"  Статус: {status_icon}\n"
            f"  Время публикации: {post.publish_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"  Текст: <i>{preview}</i>\n\n"
        )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# --- УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ ---

@router.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins_cmd(callback: CallbackQuery):
    async with async_session() as session:
        res = await session.execute(select(AdminUser))
        admins = res.scalars().all()
        
    admins_list = ""
    keyboard_buttons = []
    if not admins:
        admins_list = "<i>Нет динамических администраторов</i>"
    else:
        for idx, adm in enumerate(admins, 1):
            adm_info = f"@{adm.username}" if adm.username else f"ID: {adm.telegram_id}"
            admins_list += f"{idx}. {adm_info}\n"
            keyboard_buttons.append([InlineKeyboardButton(text=f"🗑 Удалить {adm_info}", callback_data=f"admin_del_{adm.id}")])
            
    keyboard_buttons.append([InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add_admin")])
    keyboard_buttons.append([InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")])
    
    text = (
        "👤 <b>Управление администраторами</b>\n"
        "───────────────────\n"
        "Здесь вы можете назначать новых администраторов и удалять их.\n\n"
        "Список администраторов из БД:\n"
        f"{admins_list}\n"
        "───────────────────"
    )
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_cmd(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_manage_admins")]
    ])
    await callback.message.edit_text(
        "👤 <b>Добавление администратора</b>\n"
        "───────────────────\n"
        "Отправьте юзернейм нового администратора (с @ или без) или его числовой Telegram ID:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_new_admin)
    await callback.answer()

@router.message(AdminStates.waiting_for_new_admin)
async def process_new_admin(message: Message, state: FSMContext):
    text = message.text.strip()
    username = None
    telegram_id = None
    
    if text.isdigit():
        telegram_id = int(text)
    else:
        username = text.lstrip('@')
        
    async with async_session() as session:
        if telegram_id:
            exists = await session.execute(select(AdminUser).where(AdminUser.telegram_id == telegram_id))
        else:
            exists = await session.execute(select(AdminUser).where(func.lower(AdminUser.username) == username.lower()))
            
        if exists.scalars().first():
            return await message.answer("❌ Этот администратор уже добавлен в список.", reply_markup=admin_back_keyboard())
            
        new_admin = AdminUser(telegram_id=telegram_id, username=username)
        session.add(new_admin)
        await session.commit()
        
        # Обновляем оперативную память (кэш)
        if telegram_id:
            DYNAMIC_ADMIN_IDS.add(telegram_id)
        if username:
            DYNAMIC_ADMIN_USERNAMES.add(username.lower())
        
    await message.answer(f"✅ Администратор <code>{text}</code> успешно добавлен!", reply_markup=admin_back_keyboard(), parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("admin_del_"))
async def admin_del_callback(callback: CallbackQuery):
    admin_id = int(callback.data.split("admin_del_")[1])
    async with async_session() as session:
        res = await session.execute(select(AdminUser).where(AdminUser.id == admin_id))
        admin = res.scalars().first()
        if admin:
            info = f"@{admin.username}" if admin.username else f"ID: {admin.telegram_id}"
            
            # Удаляем из оперативной памяти (кэша)
            if admin.telegram_id in DYNAMIC_ADMIN_IDS:
                DYNAMIC_ADMIN_IDS.remove(admin.telegram_id)
            if admin.username and admin.username.lower() in DYNAMIC_ADMIN_USERNAMES:
                DYNAMIC_ADMIN_USERNAMES.remove(admin.username.lower())
                
            await session.delete(admin)
            await session.commit()
            await callback.answer(f"Администратор {info} удален.", show_alert=True)
        else:
            await callback.answer("Администратор не найден.", show_alert=True)
            
    await admin_manage_admins_cmd(callback)


# --- РЕДАКТИРОВАНИЕ ПРИВЕТСТВИЯ ---

@router.callback_query(F.data == "admin_edit_welcome")
async def admin_edit_welcome_cmd(callback: CallbackQuery, state: FSMContext):
    async with async_session() as session:
        welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
        welcome_cfg = welcome_res.scalars().first()
        current_text = welcome_cfg.value if welcome_cfg else "<i>По умолчанию</i>"
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_admin")]
    ])
    
    text = (
        "✉️ <b>Редактирование приветствия</b>\n"
        "───────────────────\n"
        f"Текущий текст приветствия:\n\n{current_text}\n\n"
        "───────────────────\n"
        "Отправьте новый приветственный текст для бота (поддерживается разметка HTML):"
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_welcome_text)
    await callback.answer()

@router.message(AdminStates.waiting_for_welcome_text)
async def process_welcome_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    
    async with async_session() as session:
        welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
        welcome_cfg = welcome_res.scalars().first()
        if welcome_cfg:
            welcome_cfg.value = new_text
        else:
            welcome_cfg = BotConfig(key="welcome_text", value=new_text)
            session.add(welcome_cfg)
        await session.commit()
        
    await message.answer("✅ Приветственное сообщение успешно обновлено!", reply_markup=admin_back_keyboard())
    await state.clear()

# --- НАСТРОЙКА ИНСТРУКЦИИ ---

@router.callback_query(F.data == "admin_edit_instruction")
async def admin_edit_instruction_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session() as session:
        text_res = await session.execute(select(BotConfig).where(BotConfig.key == "info_instruction"))
        text_cfg = text_res.scalars().first()
        current_text = text_cfg.value if text_cfg else "<i>По умолчанию</i>"
        
        video_res = await session.execute(select(BotConfig).where(BotConfig.key == "instruction_video_id"))
        video_cfg = video_res.scalars().first()
        video_status = "✅ Загружено" if (video_cfg and video_cfg.value) else "❌ Не загружено"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Изменить text", callback_data="admin_edit_instruction_text")],
        [InlineKeyboardButton(text="🎥 Загрузить видео", callback_data="admin_edit_instruction_video")],
        [InlineKeyboardButton(text="🗑 Удалить видео", callback_data="admin_delete_instruction_video")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    text = (
        "🎥 <b>Настройка инструкции</b>\n"
        "───────────────────\n"
        f"<b>Текущий текст:</b>\n{current_text}\n\n"
        f"<b>Видеоинструкция:</b> {video_status}\n"
        "───────────────────\n"
        "Выберите действие:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_edit_instruction_text")
async def admin_edit_instruction_text_cmd(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_edit_instruction")]
    ])
    text = (
        "✍️ <b>Редактирование текста инструкции</b>\n"
        "───────────────────\n"
        "Отправьте новый текст инструкции (поддерживается разметка HTML):"
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_instruction_text)
    await callback.answer()

@router.message(AdminStates.waiting_for_instruction_text)
async def process_instruction_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "info_instruction"))
        cfg = cfg_res.scalars().first()
        if cfg:
            cfg.value = new_text
        else:
            cfg = BotConfig(key="info_instruction", value=new_text)
            session.add(cfg)
        await session.commit()
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Вернуться в меню инструкции", callback_data="admin_edit_instruction")]
    ])
    await message.answer("✅ Текст инструкции успешно обновлен!", reply_markup=keyboard)
    await state.clear()

@router.callback_query(F.data == "admin_edit_instruction_video")
async def admin_edit_instruction_video_cmd(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_edit_instruction")]
    ])
    text = (
        "🎥 <b>Загрузка видеоинструкции</b>\n"
        "───────────────────\n"
        "Пожалуйста, отправьте видео файл (как обычное видео) прямо в этот чат:"
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_instruction_video)
    await callback.answer()

@router.message(AdminStates.waiting_for_instruction_video, F.video)
async def process_instruction_video(message: Message, state: FSMContext):
    video_id = message.video.file_id
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "instruction_video_id"))
        cfg = cfg_res.scalars().first()
        if cfg:
            cfg.value = video_id
        else:
            cfg = BotConfig(key="instruction_video_id", value=video_id)
            session.add(cfg)
        await session.commit()
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Вернуться в меню инструкции", callback_data="admin_edit_instruction")]
    ])
    await message.answer("✅ Видеоинструкция успешно сохранена!", reply_markup=keyboard)
    await state.clear()

@router.message(AdminStates.waiting_for_instruction_video)
async def process_instruction_video_fallback(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_edit_instruction")]
    ])
    await message.answer("Пожалуйста, отправьте именно видеофайл или нажмите кнопку «❌ Отмена».", reply_markup=keyboard)

@router.callback_query(F.data == "admin_delete_instruction_video")
async def admin_delete_instruction_video_cmd(callback: CallbackQuery, state: FSMContext):
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "instruction_video_id"))
        cfg = cfg_res.scalars().first()
        if cfg:
            await session.delete(cfg)
            await session.commit()
            
    await callback.answer("Видеоинструкция удалена.", show_alert=True)
    await admin_edit_instruction_menu(callback, state)


# --- ГЕНЕРАТОР ИНВАЙТ-ССЫЛОК ---

@router.callback_query(F.data == "admin_gen_invite")
async def admin_gen_invite_cmd(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 использование", callback_data="invite_limit_1"),
            InlineKeyboardButton(text="5 использований", callback_data="invite_limit_5")
        ],
        [InlineKeyboardButton(text="Без лимита", callback_data="invite_limit_0")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    text = (
        "🔑 <b>Генератор инвайт-ссылок</b>\n"
        "───────────────────\n"
        "Создайте временную инвайт-ссылку для входа в ваш закрытый канал.\n\n"
        "Выберите лимит использований для ссылки:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("invite_limit_"))
async def process_invite_limit(callback: CallbackQuery, bot: Bot):
    limit = int(callback.data.split("invite_limit_")[1])
    
    if PRIVATE_CHANNEL_ID == 0:
        await callback.answer("Ошибка: PRIVATE_CHANNEL_ID не настроен.", show_alert=True)
        return
        
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=PRIVATE_CHANNEL_ID,
            member_limit=limit if limit > 0 else None,
            name=f"Admin_Gen_{limit}"
        )
        
        limit_text = f"{limit} использования" if limit > 0 else "без лимита"
        text = (
            "🔑 <b>Ссылка успешно сгенерирована!</b>\n"
            "───────────────────\n"
            f"Лимит: <b>{limit_text}</b>\n"
            f"Ссылка: <code>{invite.invite_link}</code>\n"
            "───────────────────"
        )
        
        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Failed to generate admin invite link: {e}")
        await callback.message.edit_text(f"❌ Ошибка генерации ссылки: {e}", reply_markup=admin_back_keyboard())
        
    await callback.answer()


# --- ЭКСПОРТ В CSV (EXCEL) ---

@router.callback_query(F.data == "admin_export_csv")
async def admin_export_csv_cmd(callback: CallbackQuery, bot: Bot):
    await callback.message.answer("⏳ Формирую подробный CSV-отчет по всей базе пользователей...")
    
    async with async_session() as session:
        from sqlalchemy.orm import selectinload
        result = await session.execute(select(User).options(selectinload(User.tickets)))
        users = result.scalars().all()
        
    if not users:
        await callback.message.answer("База данных пользователей пуста.", reply_markup=admin_back_keyboard())
        return await callback.answer()
        
    import csv
    filename = "users_export.csv"
    
    with open(filename, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Telegram ID", "Статус подписки", "Уровень", "Истекает", "Номер телефона", "Заблокирован", "Пригласил (ID)", "Билеты"])
        for u in users:
            ban_str = "Да" if u.is_banned else "Нет"
            date_str = u.expire_date.strftime('%Y-%m-%d') if u.expire_date else '—'
            active_tickets = [t.ticket_number for t in u.tickets if t.status == "Active"]
            tickets_str = ", ".join(active_tickets) if active_tickets else "—"
            writer.writerow([
                u.telegram_id,
                u.subscription_status,
                u.tier,
                date_str,
                u.phone_number or '—',
                ban_str,
                u.ref_id or '—',
                tickets_str
            ])
            
    document = FSInputFile(filename)
    await callback.message.answer_document(
        document=document,
        caption="📊 <b>Выгрузка всех пользователей (CSV)</b>\nФайл закодирован в UTF-8 (для Excel используйте разделитель точку с запятой).",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML"
    )
    
    if os.path.exists(filename):
        os.remove(filename)
        
    await callback.answer()


# --- НАСТРОЙКА БИЛЕТОВ ПО УРОВНЯМ ---

@router.callback_query(F.data == "admin_edit_tier_tickets")
async def admin_edit_tier_tickets_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    
    async with async_session() as session:
        t = await get_all_tier_tickets(session)
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1. Бесплатный ({t[1]} шт)", callback_data="admin_edit_tier_1")],
        [InlineKeyboardButton(text=f"2. Мини ({t[2]} шт)", callback_data="admin_edit_tier_2")],
        [InlineKeyboardButton(text=f"3. Стандарт ({t[3]} шт)", callback_data="admin_edit_tier_3")],
        [InlineKeyboardButton(text=f"4. ВИП ({t[4]} шт)", callback_data="admin_edit_tier_4")],
        [InlineKeyboardButton(text=f"5. ПРЕМИУМ ({t[5]} шт)", callback_data="admin_edit_tier_5")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    text = (
        "⚙️ <b>Настройка количества билетов по уровням</b>\n"
        "───────────────────\n"
        "Здесь вы можете изменить количество билетов, выдаваемое за каждый уровень доступа.\n\n"
        f"• Уровень 1 (Бесплатный): <b>{t[1]} шт.</b>\n"
        f"• Уровень 2 (Мини): <b>{t[2]} шт.</b>\n"
        f"• Уровень 3 (Стандарт): <b>{t[3]} шт.</b>\n"
        f"• Уровень 4 (ВИП): <b>{t[4]} шт.</b>\n"
        f"• Уровень 5 (ПРЕМИУМ): <b>{t[5]} шт.</b>\n"
        "───────────────────\n"
        "Выберите уровень для изменения:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_edit_tier_"))
async def admin_edit_tier_select(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    
    tier = int(callback.data.split("admin_edit_tier_")[1])
    await state.update_data(editing_tier=tier)
    await state.set_state(AdminStates.waiting_for_tier_tickets_count)
    
    from handlers.user import TIERS
    tier_name = TIERS.get(tier, {}).get("name", f"Уровень {tier}")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Отмена", callback_data="admin_edit_tier_tickets")]
    ])
    
    await callback.message.edit_text(
        f"✍ <b>Изменение количества билетов</b>\n"
        f"───────────────────\n"
        f"Тариф: <b>{tier_name}</b> (Уровень {tier})\n\n"
        f"Отправьте новое целое число билетов (от 0 и более):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_tier_tickets_count)
async def process_tier_tickets_count(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    text = message.text.strip()
    if not text.isdigit():
        return await message.answer(
            "❌ Пожалуйста, введите целое число больше или равное 0:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Отмена", callback_data="admin_edit_tier_tickets")]
            ])
        )
        
    new_count = int(text)
    data = await state.get_data()
    tier = data.get("editing_tier")
    
    if not tier:
        await state.clear()
        return await message.answer("Произошла ошибка, попробуйте снова.", reply_markup=admin_back_keyboard())
        
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == f"tickets_tier_{tier}"))
        cfg = cfg_res.scalars().first()
        if cfg:
            cfg.value = str(new_count)
        else:
            cfg = BotConfig(key=f"tickets_tier_{tier}", value=str(new_count))
            session.add(cfg)
        await session.commit()
        
    from handlers.user import TIERS
    tier_name = TIERS.get(tier, {}).get("name", f"Уровень {tier}")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад к настройкам билетов", callback_data="admin_edit_tier_tickets")],
        [InlineKeyboardButton(text="« Панель администратора", callback_data="back_to_admin")]
    ])
    
    await message.answer(
        f"✅ Количество билетов для тарифа <b>{tier_name}</b> (Уровень {tier}) "
        f"успешно изменено на <b>{new_count} шт.</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.clear()

# --- НАСТРОЙКА ОБЩЕГО ЛИМИТА БИЛЕТОВ ---

@router.callback_query(F.data == "admin_edit_tickets_limit")
async def admin_edit_tickets_limit_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    
    async with async_session() as session:
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        limit_val = limit_cfg.value if limit_cfg and limit_cfg.value else "Без лимита"
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍ Изменить лимит", callback_data="admin_set_tickets_limit_start")],
        [InlineKeyboardButton(text="🗑 Сбросить лимит (Без лимита)", callback_data="admin_clear_tickets_limit")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="back_to_admin")]
    ])
    
    text = (
        "⚙️ <b>Настройка лимита билетов</b>\n"
        "───────────────────\n"
        "Лимит ограничивает общее количество активных билетов в розыгрыше.\n"
        "Если лимит достигнут, пользователи не смогут получить новые билеты.\n\n"
        f"Текущее значение лимита: <b>{limit_val}</b>\n"
        "───────────────────\n"
        "Выберите действие:"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_set_tickets_limit_start")
async def admin_set_tickets_limit_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Отмена", callback_data="admin_edit_tickets_limit")]
    ])
    
    await callback.message.edit_text(
        "✍ <b>Изменение лимита билетов</b>\n"
        "───────────────────\n"
        "Отправьте новое максимальное количество билетов в розыгрыше (целое положительное число):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_tickets_limit)
    await callback.answer()

@router.callback_query(F.data == "admin_clear_tickets_limit")
async def admin_clear_tickets_limit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
        
    async with async_session() as session:
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        if limit_cfg:
            await session.delete(limit_cfg)
            await session.commit()
            
    await callback.answer("Лимит билетов успешно сброшен!", show_alert=True)
    await admin_edit_tickets_limit_menu(callback, state)

@router.message(AdminStates.waiting_for_tickets_limit)
async def process_tickets_limit_count(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        return await message.answer(
            "❌ Пожалуйста, введите целое положительное число больше 0:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Отмена", callback_data="admin_edit_tickets_limit")]
            ])
        )
        
    new_limit = int(text)
    
    async with async_session() as session:
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        if limit_cfg:
            limit_cfg.value = str(new_limit)
        else:
            limit_cfg = BotConfig(key="tickets_limit", value=str(new_limit))
            session.add(limit_cfg)
        await session.commit()
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« К настройке лимита", callback_data="admin_edit_tickets_limit")],
        [InlineKeyboardButton(text="« Панель администратора", callback_data="back_to_admin")]
    ])
    
    await message.answer(
        f"✅ Лимит билетов успешно изменен на <b>{new_limit} шт.</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.clear()

@router.callback_query(F.data == "admin_toggle_sales")
async def admin_toggle_sales_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        
        if sales_cfg:
            new_status = "false" if sales_cfg.value == "true" else "true"
            sales_cfg.value = new_status
        else:
            new_status = "true"
            sales_cfg = BotConfig(key="sales_stopped", value=new_status)
            session.add(sales_cfg)
            
        await session.commit()
        sales_stopped = new_status == "true"
        
        free_res = await session.execute(select(BotConfig).where(BotConfig.key == "free_ticket_enabled"))
        free_cfg = free_res.scalars().first()
        free_ticket_enabled = free_cfg.value != "false" if free_cfg else True

        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        buy_ticket_enabled = buy_cfg.value != "false" if buy_cfg else True
        
    status_text = "остановлены" if sales_stopped else "запущены"
    await callback.answer(f"Продажи успешно {status_text}!", show_alert=True)
    
    # Обновляем инлайн-кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_main_keyboard(sales_stopped, free_ticket_enabled, buy_ticket_enabled))
    except Exception:
        pass

@router.callback_query(F.data == "admin_toggle_free_ticket")
async def admin_toggle_free_ticket_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        free_res = await session.execute(select(BotConfig).where(BotConfig.key == "free_ticket_enabled"))
        free_cfg = free_res.scalars().first()
        
        if free_cfg:
            new_status = "false" if free_cfg.value != "false" else "true"
            free_cfg.value = new_status
        else:
            new_status = "false"
            free_cfg = BotConfig(key="free_ticket_enabled", value=new_status)
            session.add(free_cfg)
            
        await session.commit()
        free_ticket_enabled = new_status != "false"
        
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        sales_stopped = sales_cfg.value == "true" if sales_cfg else False

        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        buy_ticket_enabled = buy_cfg.value != "false" if buy_cfg else True
        
    status_text = "скрыта" if not free_ticket_enabled else "показана"
    await callback.answer(f"Кнопка бесплатного билета {status_text}!", show_alert=True)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_main_keyboard(sales_stopped, free_ticket_enabled, buy_ticket_enabled))
    except Exception:
        pass

@router.callback_query(F.data == "admin_toggle_buy_ticket")
async def admin_toggle_buy_ticket_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        
        if buy_cfg:
            new_status = "false" if buy_cfg.value != "false" else "true"
            buy_cfg.value = new_status
        else:
            new_status = "false"
            buy_cfg = BotConfig(key="buy_ticket_enabled", value=new_status)
            session.add(buy_cfg)
            
        await session.commit()
        buy_ticket_enabled = new_status != "false"
        
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        sales_stopped = sales_cfg.value == "true" if sales_cfg else False
        
        free_res = await session.execute(select(BotConfig).where(BotConfig.key == "free_ticket_enabled"))
        free_cfg = free_res.scalars().first()
        free_ticket_enabled = free_cfg.value != "false" if free_cfg else True
        
    status_text = "скрыта" if not buy_ticket_enabled else "показана"
    await callback.answer(f"Кнопка покупки билета {status_text}!", show_alert=True)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_main_keyboard(sales_stopped, free_ticket_enabled, buy_ticket_enabled))
    except Exception:
        pass

@router.message(Command("requests", "aprove", "approve", "zayavki"))
async def requests_cmd_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await show_requests_list(message.chat.id, message, page=1)

async def show_requests_list(chat_id: int, message_or_callback: Message | CallbackQuery, page: int = 1):
    async with async_session() as session:
        res = await session.execute(
            select(SubscriptionRequest)
            .where(SubscriptionRequest.status == "Pending")
            .order_by(SubscriptionRequest.id.asc())
        )
        reqs = res.scalars().all()
        
    total = len(reqs)
    if total == 0:
        text = "🎉 <b>Нет необработанных заявок!</b>"
        keyboard = admin_back_keyboard()
        if isinstance(message_or_callback, CallbackQuery):
            try:
                await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception:
                await message_or_callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="HTML")
        return

    items_per_page = 10
    total_pages = (total + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_reqs = reqs[start_idx:end_idx]
    
    text = (
        f"📝 <b>Ожидающие заявки ({total} шт.)</b>\n"
        f"Страница <b>{page}</b> из <b>{total_pages}</b>\n"
        "───────────────────\n"
        "Выберите заявку для просмотра чека и модерации:"
    )
    
    buttons = []
    for req in page_reqs:
        async with async_session() as s:
            u_res = await s.execute(select(User).where(User.telegram_id == req.telegram_id))
            u = u_res.scalars().first()
            username_display = f"@{u.phone_number}" if u and u.phone_number else f"ID: {req.telegram_id}"
            
        buttons.append([
            InlineKeyboardButton(
                text=f"Заявка #{req.id} ({username_display})",
                callback_data=f"vreq_{req.id}_{page}"
            )
        ])
        
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="← Пред.", callback_data=f"plist_{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="След. →", callback_data=f"plist_{page+1}"))
        
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_admin")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            try:
                await message_or_callback.message.delete()
            except Exception:
                pass
            await message_or_callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="HTML")

@router.callback_query(F.data.startswith("vreq_"))
async def view_single_request(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
        
    _, req_id, page = callback.data.split("_")
    req_id = int(req_id)
    page = int(page)
    
    async with async_session() as session:
        res = await session.execute(select(SubscriptionRequest).where(SubscriptionRequest.id == req_id))
        req = res.scalars().first()
        
        if not req:
            await callback.answer("Заявка не найдена.", show_alert=True)
            await show_requests_list(callback.message.chat.id, callback, page=page)
            return
            
        u_res = await session.execute(select(User).where(User.telegram_id == req.telegram_id))
        user = u_res.scalars().first()
        username = f"@{callback.from_user.username}" if callback.from_user.username else "Нет"
        if user and user.phone_number:
            username = f"{username} ({user.phone_number})"
            
        tickets_to_generate = await get_tier_tickets(session, req.tier)
        
    admin_text = (
        f"Заявка #{req.id} (Статус: {req.status})\n"
        f"User ID: <code>{req.telegram_id}</code>\n"
        f"Пользователь: {username}\n"
        f"Количество купонов: {tickets_to_generate} шт.\n\n"
        f"Выберите действие:"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Одобрить", callback_data=f"mappr_{req.id}_{page}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"mrejc_{req.id}_{page}")
            ],
            [InlineKeyboardButton(text="« К списку заявок", callback_data=f"plist_{page}")]
        ]
    )
    
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    await send_payment_receipt_to_admin(
        bot=bot,
        chat_id=callback.message.chat.id,
        photo_file_id=req.photo_file_id,
        caption=admin_text,
        reply_markup=keyboard
    )
    await callback.answer()

@router.callback_query(F.data.startswith("mappr_") | F.data.startswith("mrejc_"))
async def process_list_moderation(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
        
    data_parts = callback.data.split("_")
    action = data_parts[0]
    req_id = int(data_parts[1])
    page = int(data_parts[2])
    
    async with async_session() as session:
        result = await session.execute(select(SubscriptionRequest).where(SubscriptionRequest.id == req_id))
        req = result.scalars().first()

        if not req or req.status != "Pending":
            await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            await show_requests_list(callback.message.chat.id, callback, page=page)
            return

        user_result = await session.execute(select(User).where(User.telegram_id == req.telegram_id))
        user = user_result.scalars().first()
        
        if not user:
            user = User(telegram_id=req.telegram_id, subscription_status="Inactive")
            session.add(user)
            await session.flush()

        if action == "mappr":
            tickets_to_generate = await get_tier_tickets(session, req.tier)

            limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
            limit_cfg = limit_res.scalars().first()
            limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None
            
            if limit_val is not None and tickets_to_generate > 0:
                tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
                total_active = tickets_count_res.scalar() or 0
                if total_active + tickets_to_generate > limit_val:
                    limit_left = max(0, limit_val - total_active)
                    await callback.answer(
                        f"🚫 Ошибка: превышен лимит билетов!\n"
                        f"Осталось свободных билетов: {limit_left} шт.\n"
                        f"Данная заявка требует: {tickets_to_generate} шт.\n"
                        f"Сначала увеличьте лимит билетов в настройках.",
                        show_alert=True
                    )
                    return

            req.status = "Approved"
            user.subscription_status = "Active"
            user.tier = req.tier
            user.expire_date = None

            if tickets_to_generate > 0:
                await generate_tickets(session, user.telegram_id, tickets_to_generate, "regular")

            if user.ref_id and user.ref_id != user.telegram_id and not user.referral_rewarded:
                referrer_id = user.ref_id
                user.referral_rewarded = True
                
                ref_result = await session.execute(select(User).where(User.telegram_id == referrer_id))
                referrer = ref_result.scalars().first()
                if referrer:
                    await generate_tickets(session, referrer.telegram_id, 1, "bonus")
                    try:
                        await bot.send_message(referrer.telegram_id, "Вам начислен 1 бонусный купон за приглашенного друга!")
                    except Exception as e:
                        logging.error(f"Failed to notify referrer: {e}")
                    asyncio.create_task(sync_user_data(referrer_id))

            await session.commit()
            asyncio.create_task(sync_user_data(req.telegram_id))

            try:
                if PRIVATE_CHANNEL_ID != 0:
                    invite = await bot.create_chat_invite_link(
                        chat_id=PRIVATE_CHANNEL_ID,
                        member_limit=1,
                        name=f"Sub_{user.telegram_id}"
                    )
                    invite_link = invite.invite_link
                else:
                    invite_link = os.getenv("PRIVATE_CHANNEL_LINK", "Ссылка не настроена")

                text = (
                    "Поздравляем! Ваша оплата прошла!\n"
                    f"Вам начислено купонов: {tickets_to_generate} шт\n\n"
                    f"🔑 Ваша персональная ссылка на канал розыгрыша:\n{invite_link}\n\n"
                    "Подписывайтесь, чтобы не пропустить итоги розыгрыша."
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Подписаться на канал", url=invite_link)],
                        [InlineKeyboardButton(text="Поддержка", callback_data="menu_support")],
                        [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
                    ]
                )

                coupon_photo = "kupon.jpg"
                if os.path.exists(coupon_photo):
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=FSInputFile(coupon_photo),
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Failed to create invite link or notify user: {e}")
            
            back_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« К списку заявок", callback_data=f"plist_{page}")]]
            )
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ ОДОБРЕНО", reply_markup=back_keyboard)
                else:
                    await callback.message.edit_text(text=callback.message.text + "\n\n✅ ОДОБРЕНО", reply_markup=back_keyboard)
            except Exception as e:
                logging.error(f"Could not edit approval message: {e}")

        elif action == "mrejc":
            req.status = "Rejected"
            try:
                await bot.send_message(user.telegram_id, "Ваша заявка на подписку была отклонена администратором.")
            except Exception as e:
                logging.error(f"Failed to notify user about rejection: {e}")

            await session.commit()
            asyncio.create_task(sync_user_data(req.telegram_id))

            back_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« К списку заявок", callback_data=f"plist_{page}")]]
            )
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО", reply_markup=back_keyboard)
                else:
                    await callback.message.edit_text(text=callback.message.text + "\n\n❌ ОТКЛОНЕНО", reply_markup=back_keyboard)
            except Exception as e:
                logging.error(f"Could not edit rejection message: {e}")

    await callback.answer()

@router.callback_query(F.data.startswith("plist_"))
async def handle_requests_list_pagination(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split("_")[1])
    await show_requests_list(callback.message.chat.id, callback, page=page)
    await callback.answer()