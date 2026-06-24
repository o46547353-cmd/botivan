import logging
import os
import calendar
from datetime import date
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from database.db import async_session
from database.models import User, Ticket, PaymentConfig, SubscriptionRequest, BotConfig, AdminUser
from states.states import SubscriptionStates
from sqlalchemy import select, func
from services.sheets import sync_user_data
from services.tickets import generate_tickets, get_tickets_counter_text, get_tier_tickets, get_all_tier_tickets, get_ticket_plural, get_cert_plural, get_coupon_plural
import asyncio
from config import ADMIN_IDS, PRIVATE_CHANNEL_ID, REQUESTS_GROUP_ID

router = Router()

TIERS = {
    1: {"name": "Бесплатный", "price": 0, "tickets": 0},
    2: {"name": "Мини", "price": 1000, "tickets": 1},
    3: {"name": "Стандарт", "price": 5000, "tickets": 5},
    4: {"name": "ВИП", "price": 10000, "tickets": 10},
    5: {"name": "ПРЕМИУМ", "price": 100000, "tickets": 100}
}

async def send_cart_message(state: FSMContext, chat_id: int, message_or_callback: Message | CallbackQuery):
    state_data = await state.get_data()
    count = state_data.get("ticket_count", 1)
    
    async with async_session() as session:
        counter_text = await get_tickets_counter_text(session)
        
    text = (
        "🛒 <b>Оформление заказа</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Выбранное количество купонов: <b>{count}</b> шт.\n"
        "💵 Стоимость одного купона: <b>1 000 руб.</b>\n\n"
        f"💰 <b>Итого к оплате: {count * 1000} руб.</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{counter_text}\n\n"
        "Вы можете изменить количество купонов кнопками ниже или прислать нужное число сообщением:"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="-5", callback_data="cart_change_-5"),
                InlineKeyboardButton(text="-1", callback_data="cart_change_-1"),
                InlineKeyboardButton(text="+1", callback_data="cart_change_+1"),
                InlineKeyboardButton(text="+5", callback_data="cart_change_+5")
            ],
            [InlineKeyboardButton(text="Перейти к оплате 💳", callback_data=f"tier_{count}")],
            [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
        ]
    )
    
    target = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback
    
    coupon_photo = "kupon.jpg"
    if os.path.exists(coupon_photo):
        try:
            if isinstance(message_or_callback, CallbackQuery) and message_or_callback.message.photo:
                from aiogram.types import InputMediaPhoto
                await message_or_callback.message.edit_media(
                    media=InputMediaPhoto(media=FSInputFile(coupon_photo), caption=text, parse_mode="HTML"),
                    reply_markup=keyboard
                )
            else:
                if isinstance(message_or_callback, CallbackQuery):
                    try:
                        await message_or_callback.message.delete()
                    except Exception:
                        pass
                await target.answer_photo(
                    photo=FSInputFile(coupon_photo),
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
        except Exception:
            if isinstance(message_or_callback, CallbackQuery):
                try:
                    await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
                except Exception:
                    await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await target.answer_photo(photo=FSInputFile(coupon_photo), caption=text, reply_markup=keyboard, parse_mode="HTML")
    else:
        if isinstance(message_or_callback, CallbackQuery):
            try:
                await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception:
                await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=keyboard, parse_mode="HTML")

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

async def should_show_free_button(session, user_id: int) -> bool:
    # Always hidden per requirements
    return False

async def should_show_buy_button(session) -> bool:
    buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
    buy_cfg = buy_res.scalars().first()
    buy_enabled = buy_cfg.value != "false" if buy_cfg else True
    return buy_enabled

def main_menu_keyboard(show_free: bool = False, show_buy: bool = True):
    keyboard = []
    if show_free:
        keyboard.append([InlineKeyboardButton(text="Получить бесплатный купон 🎟", callback_data="menu_free_ticket")])
    if show_buy:
        keyboard.append([InlineKeyboardButton(text="ПОЛУЧИТЬ КУПОН 🎁", callback_data="menu_subscribe")])
    keyboard.extend([
        [InlineKeyboardButton(text="Мои КУПОНЫ", callback_data="menu_profile")],
        [InlineKeyboardButton(text="Информация", callback_data="menu_info")],
        [InlineKeyboardButton(text="Поддержка", callback_data="menu_support")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def free_tier_success_keyboard(invite_link: str, show_buy: bool = True):
    keyboard = []
    if show_buy:
        keyboard.append([InlineKeyboardButton(text="ПОЛУЧИТЬ КУПОН 🎁", callback_data="menu_subscribe_force")])
    keyboard.extend([
        [InlineKeyboardButton(text="Перейти на канал", url=invite_link)],
        [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_tiers_selection_text(counter_text: str) -> str:
    return (
        "Выберите кол-во купонов.\n"
        "<i>*Вы можете приобрести сразу несколько и выиграть несколько призов!</i>\n\n"
        f"{counter_text}"
    )

def user_back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
        ]
    )

@router.message(CommandStart())
async def start_cmd(message: Message):
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    ref_id = None
    if args:
        try:
            ref_id = int(args[0])
            if ref_id == message.from_user.id:
                ref_id = None
        except ValueError:
            pass

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalars().first()

        if not user:
            user = User(telegram_id=message.from_user.id, ref_id=ref_id)
            session.add(user)
            await session.commit()
            asyncio.create_task(sync_user_data(message.from_user.id))

        # Загружаем приветственный текст из БД
        welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
        welcome_cfg = welcome_res.scalars().first()
        if welcome_cfg and welcome_cfg.value:
            caption_text = welcome_cfg.value
        else:
            caption_text = (
                "Чтобы участвовать и выиграть подарок - нажмите <b>\"ПОЛУЧИТЬ КУПОН\"</b> 👇"
            )
        
        counter_text = await get_tickets_counter_text(session)
        full_text = f"{caption_text}\n\n{counter_text}"
        show_free = await should_show_free_button(session, message.from_user.id)
        show_buy = await should_show_buy_button(session)

    photo_path = "welcome.jpg"

    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(
            photo=photo,
            caption=full_text,
            reply_markup=main_menu_keyboard(show_free, show_buy),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            full_text + "\n\n<i>(Системное сообщение: файл welcome.jpg не найден в папке бота)</i>",
            reply_markup=main_menu_keyboard(show_free, show_buy),
            parse_mode="HTML"
        )

def phone_request_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Поделиться номером", request_contact=True)],
            [KeyboardButton(text="Поддержка")],
            [KeyboardButton(text="<< Назад")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_cmd(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    async with async_session() as session:
        welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
        welcome_cfg = welcome_res.scalars().first()
        if welcome_cfg and welcome_cfg.value:
            caption_text = welcome_cfg.value
        else:
            caption_text = (
                "Чтобы участвовать и выиграть подарок - нажмите <b>\"ПОЛУЧИТЬ КУПОН\"</b> 👇"
            )
        
        counter_text = await get_tickets_counter_text(session)
        full_text = f"{caption_text}\n\n{counter_text}"
        show_free = await should_show_free_button(session, callback.from_user.id)
        show_buy = await should_show_buy_button(session)
    
    photo_path = "welcome.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await callback.message.answer_photo(
            photo=photo,
            caption=full_text,
            reply_markup=main_menu_keyboard(show_free, show_buy),
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            full_text,
            reply_markup=main_menu_keyboard(show_free, show_buy),
            parse_mode="HTML"
        )
    await callback.answer()

@router.callback_query(F.data.in_({"menu_subscribe", "menu_subscribe_force"}))
async def process_subscription_cmd(callback: CallbackQuery, state: FSMContext):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalars().first()
        
        # Проверяем стоп-продажи
        sales_res = await session.execute(select(BotConfig).where(BotConfig.key == "sales_stopped"))
        sales_cfg = sales_res.scalars().first()
        sales_stopped = sales_cfg.value == "true" if sales_cfg else False

        # Проверяем, включена ли покупка билета
        buy_res = await session.execute(select(BotConfig).where(BotConfig.key == "buy_ticket_enabled"))
        buy_cfg = buy_res.scalars().first()
        buy_enabled = buy_cfg.value != "false" if buy_cfg else True
        
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    if not buy_enabled:
        await callback.answer("Покупка билетов в данный момент недоступна.", show_alert=True)
        return

    if sales_stopped:
        warning_text = (
            "🛑 <b>Продажи билетов временно остановлены</b> 🛑\n\n"
            "Уважаемые участники! В данный момент продажи билетов на текущий розыгрыш временно приостановлены. 🎁\n\n"
            "📢 Следите за новостями и анонсами в закрытом канале. Мы обязательно сообщим о возобновлении продаж!\n\n"
            "Если у вас возникли вопросы, вы всегда можете написать в поддержку."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
            ]
        )
        try:
            await callback.message.edit_text(warning_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.delete()
            await callback.message.answer(warning_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
        return

    # Если у пользователя уже есть активный доступ, пишем, что он уже в канале
    if user.subscription_status == "Active" and callback.data != "menu_subscribe_force":
        async with async_session() as session:
            tickets_result = await session.execute(
                select(Ticket).where(Ticket.user_id == callback.from_user.id, Ticket.status == "Active")
            )
            tickets = tickets_result.scalars().all()
            tickets_count = len(tickets)
            
            draw_res = await session.execute(select(BotConfig).where(BotConfig.key == "draw_date"))
            draw_cfg = draw_res.scalars().first()
            draw_date_str = draw_cfg.value if draw_cfg else "19 июля 2026"

        if len(tickets) == 1:
            ticket_list_str = f"🎟 <b>Номер купона:</b> <code>{tickets[0].ticket_number}</code>\n\n"
        else:
            ticket_texts = ", ".join([f"<code>{t.ticket_number}</code>" for t in tickets])
            ticket_list_str = f"🎟 <b>Номера купонов:</b> {ticket_texts}\n\n" if tickets else ""

        warning_text = (
            "Поздравляем! Вы уже зарегистрированы на участие!\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎟 <b>Активных купонов:</b> {tickets_count} шт.\n"
            f"🗓 <b>Итоги розыгрыша:</b> {draw_date_str}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{ticket_list_str}"
            "Хотите еще получить купоны?"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="ПОЛУЧИТЬ КУПОН 🎁", callback_data="menu_subscribe_force")],
                [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
            ]
        )

        try:
            await callback.message.edit_text(warning_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.delete()
            await callback.message.answer(warning_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
        return

    if not user.phone_number:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "1. Укажите Ваш номер телефона - на него будем звонить победителю.\n"
            "2. Оплатите купон по ссылке или по QR через СБП.\n"
            "3. Пришлите скриншот чека/документ в этот чат.\n"
            "4. Мы проверим платеж и вы получите персональный № купона для участия в розыгрыше.\n\n"
            "<b>Напишите/поделитесь номером телефона 👇</b>",
            reply_markup=phone_request_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(SubscriptionStates.waiting_for_phone)
        await callback.answer()
        return

    await state.update_data(ticket_count=1)
    await send_cart_message(state, callback.message.chat.id, callback)
    await state.set_state(SubscriptionStates.waiting_for_tier_selection)
    await callback.answer()

import re

def clean_phone_number(text: str) -> str | None:
    digits = re.sub(r'\D', '', text)
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11:
        if digits.startswith('8'):
            return f"+7{digits[1:]}"
        elif digits.startswith('7'):
            return f"+{digits}"
        else:
            return f"+{digits}"
    if text.strip().startswith('+') and 9 <= len(digits) <= 15:
        return f"+{digits}"
    if 9 <= len(digits) <= 15:
        return f"+{digits}"
    return None

async def save_phone_and_continue(phone: str, message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    flow = data.get("flow")
    
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalars().first()
        if user:
            user.phone_number = phone
            await session.commit()
            
    asyncio.create_task(sync_user_data(message.from_user.id))
    
    await message.answer(
        "✅ Ваш номер телефона успешно подтвержден!", 
        reply_markup=ReplyKeyboardRemove()
    )
    


    await state.update_data(ticket_count=1)
    await send_cart_message(state, message.chat.id, message)
    await state.set_state(SubscriptionStates.waiting_for_tier_selection)

@router.message(SubscriptionStates.waiting_for_phone, F.contact)
async def process_phone_sharing(message: Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    await save_phone_and_continue(phone, message, state, bot)

@router.message(SubscriptionStates.waiting_for_phone, F.text & (F.text != "<< Назад") & (F.text != "Поддержка"))
async def process_phone_manual(message: Message, state: FSMContext, bot: Bot):
    phone = clean_phone_number(message.text)
    if phone:
        await save_phone_and_continue(phone, message, state, bot)
    else:
        await message.answer(
            "Некорректный формат номера телефона.\n"
            "Пожалуйста, напишите номер телефона в формате <b>+7 999 555 22 22</b> или воспользуйтесь кнопкой «Поделиться номером» ниже.",
            reply_markup=phone_request_keyboard(),
            parse_mode="HTML"
        )

@router.callback_query(F.data == "menu_free_ticket")
async def process_free_ticket_cmd(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer("К сожалению, бесплатные купоны больше недоступны. Вы можете приобрести купон в меню.", show_alert=True)

@router.message(SubscriptionStates.waiting_for_phone, F.text == "<< Назад")
async def cancel_phone_sharing(message: Message, state: FSMContext):
    data = await state.get_data()
    flow = data.get("flow")
    await state.clear()
    
    async with async_session() as session:
        show_free = await should_show_free_button(session, message.from_user.id)
        show_buy = await should_show_buy_button(session)
        
    if flow == "free":
        await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
        async with async_session() as session:
            welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
            welcome_cfg = welcome_res.scalars().first()
            if welcome_cfg and welcome_cfg.value:
                caption_text = welcome_cfg.value
            else:
                caption_text = "Чтобы участвовать и выиграть подарок - нажмите <b>\"ПОЛУЧИТЬ КУПОН\"</b> 👇"
            
            counter_text = await get_tickets_counter_text(session)
            full_text = f"{caption_text}\n\n{counter_text}"
            
        photo_path = "welcome.jpg"
        if os.path.exists(photo_path):
            await message.answer_photo(
                photo=FSInputFile(photo_path),
                caption=full_text,
                reply_markup=main_menu_keyboard(show_free, show_buy),
                parse_mode="HTML"
            )
        else:
            await message.answer(
                full_text,
                reply_markup=main_menu_keyboard(show_free, show_buy),
                parse_mode="HTML"
            )
    else:
        await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
        await state.update_data(ticket_count=1)
        await send_cart_message(state, message.chat.id, message)
        await state.set_state(SubscriptionStates.waiting_for_tier_selection)

@router.message(SubscriptionStates.waiting_for_phone, F.text == "Поддержка")
async def process_phone_support(message: Message):
    admin_username = os.getenv("ADMIN_USERNAME", "@podarki_support")
    await message.answer(f"По всем вопросам обращайтесь к администратору: {admin_username}")

@router.message(SubscriptionStates.waiting_for_phone)
async def process_phone_sharing_fallback(message: Message):
    await message.answer(
        "Пожалуйста, напишите ваш номер телефона в формате <b>+7 999 555 22 22</b> или воспользуйтесь кнопкой «Поделиться номером» ниже для отправки контакта.",
        reply_markup=phone_request_keyboard(),
        parse_mode="HTML"
    )

async def send_payment_qr(message_or_callback, tier: int, session):
    result = await session.execute(select(PaymentConfig).where(PaymentConfig.tier == 2))
    config_obj = result.scalars().first()
    
    welcome_res = await session.execute(select(BotConfig).where(BotConfig.key == "welcome_text"))
    welcome_cfg = welcome_res.scalars().first()
    welcome_text = welcome_cfg.value if welcome_cfg and welcome_cfg.value else ""

    payment_instructions = config_obj.text if config_obj and config_obj.text else (
        "<b>Оплатите купон по QR через СБП.</b>\n\n"
        "И обязательно сделайте скриншот чека и пришлите в этот чат."
    )
    
    sum_to_pay = tier * 1000
    caption_text = (
        f"💳 <b>Оплата заказа</b>\n"
        f"🎟 Количество купонов: <b>{tier} шт.</b>\n"
        f"💰 Итого к оплате: <b>{sum_to_pay} руб.</b>\n\n"
        f"{payment_instructions}"
    )
    
    if welcome_text:
        text = f"{welcome_text}\n\n{caption_text}"
    else:
        text = caption_text

    sbp_res = await session.execute(select(BotConfig).where(BotConfig.key == "sbp_static_link"))
    sbp_cfg = sbp_res.scalars().first()
    
    photo = None
    if sbp_cfg and sbp_cfg.value:
        base_link = sbp_cfg.value.strip()
        sum_kopecks = sum_to_pay * 100
        separator = "&" if "?" in base_link else "?"
        dynamic_link = f"{base_link}{separator}sum={sum_kopecks}&cur=RUB&type=01"
        
        import urllib.parse
        encoded_link = urllib.parse.quote_plus(dynamic_link)
        photo = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded_link}"
    else:
        global_qr_res = await session.execute(select(BotConfig).where(BotConfig.key == "global_payment_qr"))
        global_qr_cfg = global_qr_res.scalars().first()
        if global_qr_cfg and global_qr_cfg.value:
            photo = global_qr_cfg.value

    if not photo and config_obj:
        photo = config_obj.photo_file_id
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплачено - прислать чек", callback_data="payment_done")],
            [
                InlineKeyboardButton(text="🎥 Инструкция", callback_data="info_instruction"),
                InlineKeyboardButton(text="Поддержка", callback_data="menu_support")
            ],
            [InlineKeyboardButton(text="<< Назад", callback_data="menu_subscribe_force")]
        ]
    )
    
    target = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback
    
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.delete()
        except Exception:
            pass

    if photo:
        await target.answer_photo(photo, caption=text, reply_markup=keyboard, parse_mode="HTML")
    else:
        fallback_qr = "qr.jpg"
        if os.path.exists(fallback_qr):
            await target.answer_photo(FSInputFile(fallback_qr), caption=text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=keyboard, parse_mode="HTML")

@router.callback_query(SubscriptionStates.waiting_for_tier_selection, F.data.startswith("tier_"))
async def process_tier_selection(callback: CallbackQuery, state: FSMContext, bot: Bot):
    tier = int(callback.data.split("_")[1])
    
    async with async_session() as session:
        tickets_to_buy = await get_tier_tickets(session, tier)
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None
        
        if limit_val is not None and tickets_to_buy > 0:
            tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
            total_active = tickets_count_res.scalar() or 0
            if total_active + tickets_to_buy > limit_val:
                limit_left = max(0, limit_val - total_active)
                if limit_left == 0:
                    await callback.answer("⚠️ К сожалению, все купоны на этот розыгрыш уже распроданы!", show_alert=True)
                else:
                    await callback.answer(f"⚠️ Извините, осталось всего {limit_left} доступных купонов. Выберите другой тариф.", show_alert=True)
                return

    await state.update_data(selected_tier=tier)
    


    async with async_session() as session:
        await send_payment_qr(callback, tier, session)

    await state.set_state(SubscriptionStates.waiting_for_payment_receipt)
    await callback.answer()

from services.media import send_payment_receipt_to_admin
import json

@router.message(SubscriptionStates.waiting_for_payment_receipt, F.photo | F.video | F.document)
async def process_payment_receipt(message: Message, state: FSMContext, bot: Bot, album: list[Message] = None):
    data = await state.get_data()
    tier = data.get("selected_tier")
    
    if album:
        media_items = []
        for msg in album:
            if msg.photo:
                media_items.append({"type": "photo", "file_id": msg.photo[-1].file_id})
            elif msg.video:
                media_items.append({"type": "video", "file_id": msg.video.file_id})
            elif msg.document:
                media_items.append({"type": "document", "file_id": msg.document.file_id})
        photo_id = json.dumps(media_items) if media_items else None
    else:
        media_items = []
        if message.photo:
            media_items.append({"type": "photo", "file_id": message.photo[-1].file_id})
        elif message.video:
            media_items.append({"type": "video", "file_id": message.video.file_id})
        elif message.document:
            media_items.append({"type": "document", "file_id": message.document.file_id})
        photo_id = json.dumps(media_items) if media_items else None
    
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет"

    async with async_session() as session:
        req = SubscriptionRequest(telegram_id=message.from_user.id, tier=tier, photo_file_id=photo_id)
        session.add(req)
        await session.commit()
        await session.refresh(req)

        tickets_to_generate = await get_tier_tickets(session, tier)
        admin_text = (
            f"Новая заявка на подписку!\n"
            f"User ID: <code>{message.from_user.id}</code>\n"
            f"Ник: {username}\n"
            f"Количество купонов: {tickets_to_generate} шт\n"
            f"ID заявки: {req.id}"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Одобрить", callback_data=f"approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"reject_{req.id}")
                ]
            ]
        )
        
        # Получаем список статических и динамических админов
        admin_ids = list(ADMIN_IDS)
        res_adm = await session.execute(select(AdminUser.telegram_id).where(AdminUser.telegram_id != None))
        for row in res_adm.scalars().all():
            if row not in admin_ids:
                admin_ids.append(row)

        if REQUESTS_GROUP_ID != 0:
            try:
                await send_payment_receipt_to_admin(
                    bot=bot,
                    chat_id=REQUESTS_GROUP_ID,
                    photo_file_id=photo_id,
                    caption=admin_text,
                    reply_markup=keyboard
                )
            except Exception as e:
                logging.error(f"Failed to send to requests group {REQUESTS_GROUP_ID}: {e}")

        for admin_id in admin_ids:
            if admin_id != 0:
                try:
                    await send_payment_receipt_to_admin(
                        bot=bot,
                        chat_id=admin_id,
                        photo_file_id=photo_id,
                        caption=admin_text,
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logging.error(f"Failed to send to admin {admin_id}: {e}")
                    
        show_free = await should_show_free_button(session, message.from_user.id)
        show_buy = await should_show_buy_button(session)

    await message.answer("Ваш чек отправлен на проверку администратору. Ожидайте подтверждения.", reply_markup=main_menu_keyboard(show_free, show_buy))
    await state.clear()

@router.message(SubscriptionStates.waiting_for_payment_receipt)
async def process_payment_receipt_fallback(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="<< Назад к выбору количества", callback_data="menu_subscribe_force")]
        ]
    )
    await message.answer(
        "Пожалуйста, отправьте скриншот оплаты в виде фотографии/видео или PDF-файла, "
        "чтобы система смогла передать его администратору на проверку.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "menu_profile")
async def profile_cmd(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalars().first()
        
        if not user:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return

        tickets_result = await session.execute(select(Ticket).where(Ticket.user_id == callback.from_user.id, Ticket.status == "Active"))
        tickets = tickets_result.scalars().all()
        
        draw_res = await session.execute(select(BotConfig).where(BotConfig.key == "draw_date"))
        draw_cfg = draw_res.scalars().first()
        draw_date_str = draw_cfg.value if draw_cfg else "19 июля 2026"

    if len(tickets) == 1:
        ticket_list_str = f"🎟 <b>Номер купона:</b> <code>{tickets[0].ticket_number}</code>\n\n"
    else:
        ticket_texts = ", ".join([f"<code>{t.ticket_number}</code>" for t in tickets])
        ticket_list_str = f"🎟 <b>Номера купонов:</b> {ticket_texts}\n\n" if tickets else ""
    
    profile_text = (
        f"🎟 <b>Активных купонов:</b> {len(tickets)} шт.\n"
        f"🗓 <b>Итоги розыгрыша:</b> {draw_date_str}\n\n"
        f"{ticket_list_str}"
        "Выиграть приз может каждый, у кого есть скидочный купон на 1000р от SKYNET VR.\n\n"
        "<i>*купон дает скидку на любое тарифное мероприятие в SKYNET VR.</i>"
    )

    coupon_photo = "kupon.jpg"
    if os.path.exists(coupon_photo):
        try:
            if callback.message.photo:
                from aiogram.types import InputMediaPhoto
                await callback.message.edit_media(
                    media=InputMediaPhoto(media=FSInputFile(coupon_photo), caption=profile_text, parse_mode="HTML"),
                    reply_markup=user_back_keyboard()
                )
            else:
                await callback.message.delete()
                await callback.message.answer_photo(
                    photo=FSInputFile(coupon_photo),
                    caption=profile_text,
                    reply_markup=user_back_keyboard(),
                    parse_mode="HTML"
                )
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(
                photo=FSInputFile(coupon_photo),
                caption=profile_text,
                reply_markup=user_back_keyboard(),
                parse_mode="HTML"
            )
    else:
        try:
            await callback.message.edit_text(profile_text, reply_markup=user_back_keyboard(), parse_mode="HTML")
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(profile_text, reply_markup=user_back_keyboard(), parse_mode="HTML")
    await callback.answer()

def referral_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Таблица лидеров", callback_data="menu_referral_leaderboard")],
            [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
        ]
    )

@router.callback_query(F.data == "menu_referral")
async def referral_cmd(callback: CallbackQuery, bot: Bot):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    
    text = (
        "<b>Пригласите друга по этой ссылке и получите бонусный билет:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "Если друг получит билет, вы получите <b>+1 билет на текущий розыгрыш в подарок</b> — это увеличит шансы на победу!"
    )
    
    try:
        await callback.message.edit_text(text, reply_markup=referral_keyboard(), parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=referral_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "menu_referral_leaderboard")
async def referral_leaderboard_cmd(callback: CallbackQuery, bot: Bot):
    async with async_session() as session:
        # Получаем топ-10 пользователей по количеству приглашенных рефералов
        query = (
            select(User.ref_id, func.count(User.id).label('ref_count'))
            .where(User.ref_id != None, User.referral_rewarded == True)
            .group_by(User.ref_id)
            .order_by(func.count(User.id).desc())
            .limit(10)
        )
        result = await session.execute(query)
        rows = result.all()
        
        # Статистика текущего пользователя
        user_refs_res = await session.execute(
            select(func.count(User.id)).where(User.ref_id == callback.from_user.id, User.referral_rewarded == True)
        )
        user_refs_count = user_refs_res.scalar() or 0
        
    leaderboard_text = "🏆 <b>Таблица лидеров рефералов:</b>\n\n"
    
    if not rows:
        leaderboard_text += "Пока здесь пусто. Будьте первым!\n"
    else:
        for idx, row in enumerate(rows, 1):
            ref_user_id = row[0]
            count = row[1]
            try:
                member = await bot.get_chat(ref_user_id)
                name = f"@{member.username}" if member.username else (member.first_name or f"Пользователь {ref_user_id}")
            except Exception:
                name = f"Пользователь {str(ref_user_id)[:4]}***"
            
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            medal = medals.get(idx, f"{idx}.")
            leaderboard_text += f"{medal} {name} — <b>{count}</b> приглашенных\n"
            
    leaderboard_text += f"\n👥 <b>Вы пригласили:</b> {user_refs_count} друзей.\n\n"
    leaderboard_text += "🎯 <b>Реферальные цели:</b>\n"
    leaderboard_text += "• Пригласите 5 друзей — получите статус топ-пригласителя!\n"
    if user_refs_count >= 5:
        leaderboard_text += "🔥 <b>Цель достигнута! Вы пригласили более 5 друзей!</b>\n"
    else:
        leaderboard_text += f"⏳ До цели осталось пригласить: {5 - user_refs_count} друзей.\n"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« Назад к программе", callback_data="menu_referral")],
            [InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")]
        ]
    )
    
    try:
        await callback.message.edit_text(leaderboard_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(leaderboard_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "menu_support")
async def support_cmd(callback: CallbackQuery):
    admin_username = os.getenv("ADMIN_USERNAME", "@podarki_support")
    text = f"По всем вопросам обращайтесь к администратору: {admin_username}"
    
    try:
        await callback.message.edit_text(text, reply_markup=user_back_keyboard())
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=user_back_keyboard())
    await callback.answer()

# --- РАЗДЕЛ ИНФОРМАЦИЯ ---
@router.callback_query(F.data == "menu_info")
async def info_menu_cmd(callback: CallbackQuery):
    text = "<b>Раздел информация</b>"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Инструкция", callback_data="info_instruction")],
            [InlineKeyboardButton(text="Политика конф-ти", callback_data="info_policy")],
            [InlineKeyboardButton(text="Оферта", callback_data="info_offer")],
            [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_instruction")
async def info_instruction_cmd(callback: CallbackQuery):
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "info_instruction"))
        cfg = cfg_res.scalars().first()
        
        video_res = await session.execute(select(BotConfig).where(BotConfig.key == "instruction_video_id"))
        video_cfg = video_res.scalars().first()
        video_file_id = video_cfg.value if video_cfg else None

    text = cfg.value if cfg and cfg.value else (
        "<b>Инструкция по участию в розыгрыше:</b>\n\n"
        "1. Нажмите на раздел <b>\"ПОЛУЧИТЬ КУПОН\"</b>.\n"
        "2. Укажите/подтвердите свой номер телефона через +7.\n"
        "3. Оплатите купоны по QR-коду через СБП.\n"
        "4. Отправьте скриншот чека об оплате в чат.\n"
        "5. Администратор подтвердит оплату и вы получите купоны и персональную ссылку на закрытый канал розыгрыша."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="<< Назад к информации", callback_data="menu_info")],
            [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
        ]
    )

    if video_file_id:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer_video(
            video=video_file_id,
            caption=text[:1024],
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    else:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_policy")
async def info_policy_cmd(callback: CallbackQuery):
    async with async_session() as session:
        cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "info_policy"))
        cfg = cfg_res.scalars().first()
    text = cfg.value if cfg and cfg.value else (
        "<b>Политика конфиденциальности:</b>\n\n"
        "Мы гарантируем безопасность личных данных.\n"
        "Ваш номер телефона и чек об оплате используются исключительно для проведения розыгрыша и связи с победителем.\n\n"
        "Подробнее по ссылке: https://skynet-vr.ru/policy"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="<< Назад к информации", callback_data="menu_info")],
            [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_offer")
async def info_offer_cmd(callback: CallbackQuery):
    offer_file = "Публичная_оферта_о_приобритении_купона.pdf"
    if os.path.exists(offer_file):
        try:
            await callback.message.answer_document(
                document=FSInputFile(offer_file),
                caption="📄 <b>Публичная оферта о приобретении купона</b>",
                parse_mode="HTML"
            )
            await callback.answer()
        except Exception as e:
            logging.error(f"Failed to send offer PDF: {e}")
            await callback.answer("Ошибка при отправке файла оферты.", show_alert=True)
    else:
        async with async_session() as session:
            cfg_res = await session.execute(select(BotConfig).where(BotConfig.key == "info_offer"))
            cfg = cfg_res.scalars().first()
        text = cfg.value if cfg and cfg.value else (
            "<b>Договор оферты:</b>\n\n"
            "Участие в розыгрыше является добровольным. Приобретая купоны, вы подтверждаете свое согласие с правилами участия и проведения розыгрышей."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="<< Назад к информации", callback_data="menu_info")],
                [InlineKeyboardButton(text="<< Назад в меню", callback_data="back_to_menu")]
            ]
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()

@router.callback_query(F.data == "payment_done")
async def process_payment_done_click(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "Пожалуйста, отправьте скриншот чека (в виде фото или видео) прямо в этот чат."
    )

@router.callback_query(SubscriptionStates.waiting_for_tier_selection, F.data.startswith("cart_change_"))
async def process_cart_change(callback: CallbackQuery, state: FSMContext):
    change = int(callback.data.split("_")[2])
    state_data = await state.get_data()
    current_count = state_data.get("ticket_count", 1)
    
    new_count = current_count + change
    if new_count < 1:
        new_count = 1
    elif new_count > 100:
        new_count = 100
        
    async with async_session() as session:
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None
        
        if limit_val is not None:
            tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
            total_active = tickets_count_res.scalar() or 0
            if total_active + new_count > limit_val:
                limit_left = max(0, limit_val - total_active)
                if limit_left <= 0:
                    await callback.answer("⚠️ К сожалению, все купоны уже распроданы!", show_alert=True)
                    return
                new_count = limit_left
                await callback.answer(f"⚠️ Осталось всего {limit_left} доступных купонов.", show_alert=True)
                
    await state.update_data(ticket_count=new_count)
    await send_cart_message(state, callback.message.chat.id, callback)
    await callback.answer()

@router.message(SubscriptionStates.waiting_for_tier_selection)
async def process_cart_text_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "<< Назад в меню" or text == "/start" or text.startswith("/"):
        return
        
    try:
        count = int(text)
        if count < 1 or count > 100:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите корректное число купонов (от 1 до 100):")
        return
        
    async with async_session() as session:
        limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
        limit_cfg = limit_res.scalars().first()
        limit_val = int(limit_cfg.value) if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit() else None
        
        if limit_val is not None:
            tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
            total_active = tickets_count_res.scalar() or 0
            if total_active + count > limit_val:
                limit_left = max(0, limit_val - total_active)
                if limit_left <= 0:
                    await message.answer("⚠️ К сожалению, все купоны уже распроданы!")
                    return
                count = limit_left
                await message.answer(f"⚠️ Осталось всего {limit_left} доступных купонов. Выбрано максимальное количество.")
                
    await state.update_data(ticket_count=count)
    await send_cart_message(state, message.chat.id, message)