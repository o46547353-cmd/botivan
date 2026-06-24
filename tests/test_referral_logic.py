import unittest
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from database.db import Base
import database.db
from database.models import User, Ticket, BotConfig, SubscriptionRequest, PaymentConfig
from handlers.user import process_free_ticket_cmd, process_tier_selection
from handlers.admin import process_subscription_request
from states.states import SubscriptionStates
from aiogram.fsm.context import FSMContext

class TestReferralFreeLogic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create an in-memory async SQLite database for testing
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        
        # Override the global async_session in database.db, handlers.user and handlers.admin
        database.db.async_session = self.async_session
        import handlers.user
        handlers.user.async_session = self.async_session
        import handlers.admin
        handlers.admin.async_session = self.async_session
        
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            
    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_referral_on_payment_approval(self):
        referrer_id = 99999
        friend_id = 11111
        
        async with self.async_session() as session:
            session.add(User(telegram_id=referrer_id))
            session.add(User(telegram_id=friend_id, ref_id=referrer_id))
            
            # Create a pending subscription request for 5 tickets (tier = 5)
            req = SubscriptionRequest(telegram_id=friend_id, tier=5, status="Pending")
            session.add(req)
            await session.commit()
            req_id = req.id

        callback = AsyncMock()
        callback.from_user.id = 88888  # admin ID
        callback.data = f"approve_{req_id}"
        callback.message.edit_text = AsyncMock()
        callback.message.edit_caption = AsyncMock()
        
        bot = AsyncMock()
        invite_mock = MagicMock()
        invite_mock.invite_link = "https://t.me/test_link"
        bot.create_chat_invite_link.return_value = invite_mock

        # Mock the is_admin check
        import handlers.admin
        original_is_admin = handlers.admin.is_admin
        handlers.admin.is_admin = lambda uid: True

        try:
            await process_subscription_request(callback, bot)
        finally:
            handlers.admin.is_admin = original_is_admin

        async with self.async_session() as session:
            # Referrer should get 1 bonus ticket
            tickets = (await session.execute(select_tickets_query(referrer_id))).scalars().all()
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].ticket_type, "bonus")
            
            # Friend should get 5 tickets (since tier = 5 means 5 tickets now)
            friend_tickets = (await session.execute(select_tickets_query(friend_id))).scalars().all()
            self.assertEqual(len(friend_tickets), 5)
            for t in friend_tickets:
                self.assertEqual(t.ticket_type, "regular")
                
            friend_user = (await session.execute(select_user_query(friend_id))).scalars().first()
            self.assertEqual(friend_user.ref_id, referrer_id)
            self.assertTrue(friend_user.referral_rewarded)
            
        bot.send_message.assert_any_call(referrer_id, "Вам начислен 1 бонусный купон за приглашенного друга!")

    async def test_free_ticket_cmd_returns_alert(self):
        friend_id = 22222
        
        async with self.async_session() as session:
            session.add(User(telegram_id=friend_id))
            await session.commit()
            
        callback = AsyncMock()
        callback.from_user.id = friend_id
        
        state = AsyncMock()
        bot = AsyncMock()
        
        await process_free_ticket_cmd(callback, state, bot)
        
        # Verify that callback.answer was called with the message that the promotion has ended
        callback.answer.assert_called_once_with(
            "К сожалению, бесплатные купоны больше недоступны. Вы можете приобрести купон в меню.",
            show_alert=True
        )

    async def test_tier_selection_goes_to_payment_qr_even_for_tier_1(self):
        friend_id = 33333
        
        async with self.async_session() as session:
            session.add(User(telegram_id=friend_id, phone_number="+79991112233"))
            session.add(PaymentConfig(tier=2, text="Test payment text", photo_file_id="photo123"))
            await session.commit()
            
        from aiogram.types import CallbackQuery
        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = MagicMock()
        callback.from_user.id = friend_id
        callback.data = "tier_1"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        
        state = AsyncMock()
        bot = AsyncMock()
        
        await process_tier_selection(callback, state, bot)
        
        # Verify that state sets waiting_for_payment_receipt instead of giving free access
        state.set_state.assert_called_once_with(SubscriptionStates.waiting_for_payment_receipt)
        # Verify it answered with photo (the payment QR flow)
        callback.message.answer_photo.assert_called_once()

def select_tickets_query(user_id):
    from sqlalchemy import select
    return select(Ticket).where(Ticket.user_id == user_id)

def select_user_query(user_id):
    from sqlalchemy import select
    return select(User).where(User.telegram_id == user_id)

if __name__ == "__main__":
    unittest.main()
