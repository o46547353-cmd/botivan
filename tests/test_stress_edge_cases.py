import unittest
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func
from database.db import Base
import database.db
from database.models import User, Ticket, BotConfig, SubscriptionRequest, PaymentConfig
from handlers.user import process_cart_text_input, process_cart_change, process_tier_selection
from handlers.admin import process_subscription_request
from states.states import SubscriptionStates
from aiogram.types import CallbackQuery, Message

class TestStressEdgeCases(unittest.IsolatedAsyncioTestCase):
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

    def _create_message_mock(self, text):
        message = AsyncMock(spec=Message)
        message.text = text
        message.chat = MagicMock()
        message.chat.id = 12345
        message.answer = AsyncMock()
        message.answer_photo = AsyncMock()
        return message

    def _create_callback_mock(self, data, count=1):
        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = MagicMock()
        callback.from_user.id = 12345
        callback.data = data
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        return callback

    # 1. Invalid Cart Text Inputs
    async def test_invalid_text_inputs_in_cart(self):
        invalid_inputs = ["abc", "0", "-5", "101", "5.5", "   "]
        for inp in invalid_inputs:
            message = self._create_message_mock(inp)
            state = AsyncMock()
            
            await process_cart_text_input(message, state)
            
            # Assert state did not update ticket_count and message.answer was called with the warning
            state.update_data.assert_not_called()
            message.answer.assert_called_with("⚠️ Пожалуйста, введите корректное число купонов (от 1 до 100):")

    # 2. Valid Cart Text Inputs
    async def test_valid_text_inputs_in_cart(self):
        valid_inputs = [("1", 1), ("50", 50), ("100", 100)]
        for inp, expected in valid_inputs:
            message = self._create_message_mock(inp)
            state = AsyncMock()
            state.get_data.return_value = {"ticket_count": expected}
            
            await process_cart_text_input(message, state)
            
            state.update_data.assert_called_with(ticket_count=expected)

    # 3. Cart Change Callbacks Underflow / Overflow
    async def test_cart_change_callbacks_underflow_overflow(self):
        # Case A: Start count = 1, change = -1 -> should cap at 1
        callback = self._create_callback_mock("cart_change_-1")
        state = AsyncMock()
        state.get_data.return_value = {"ticket_count": 1}
        
        await process_cart_change(callback, state)
        state.update_data.assert_called_with(ticket_count=1)

        # Case B: Start count = 1, change = -5 -> should cap at 1
        callback = self._create_callback_mock("cart_change_-5")
        state = AsyncMock()
        state.get_data.return_value = {"ticket_count": 1}
        
        await process_cart_change(callback, state)
        state.update_data.assert_called_with(ticket_count=1)

        # Case C: Start count = 98, change = 5 -> should cap at 100
        callback = self._create_callback_mock("cart_change_+5")
        state = AsyncMock()
        state.get_data.return_value = {"ticket_count": 98}
        
        await process_cart_change(callback, state)
        state.update_data.assert_called_with(ticket_count=100)

        # Case D: Start count = 100, change = 1 -> should cap at 100
        callback = self._create_callback_mock("cart_change_+1")
        state = AsyncMock()
        state.get_data.return_value = {"ticket_count": 100}
        
        await process_cart_change(callback, state)
        state.update_data.assert_called_with(ticket_count=100)

    # 4. Ticket Limit Edge Cases
    async def test_ticket_limit_cap_in_cart(self):
        # Set database tickets limit to 10
        async with self.async_session() as session:
            session.add(BotConfig(key="tickets_limit", value="10"))
            # Generate 8 active tickets (only 2 left)
            for i in range(8):
                session.add(Ticket(user_id=11111, ticket_number=f"T{i}", status="Active"))
            await session.commit()

        # User tries to text input "5" in cart
        message = self._create_message_mock("5")
        state = AsyncMock()
        # Mock get_data to simulate updated state
        state.get_data.return_value = {"ticket_count": 2}
        
        await process_cart_text_input(message, state)
        
        # Verify it tells the user only 2 tickets are left and caps it
        message.answer.assert_called_with("⚠️ Осталось всего 2 доступных купонов. Выбрано максимальное количество.")
        state.update_data.assert_called_with(ticket_count=2)

    # 5. Duplicate Admin Approval Prevention
    async def test_admin_approval_duplicate(self):
        user_id = 12345
        async with self.async_session() as session:
            session.add(User(telegram_id=user_id))
            req = SubscriptionRequest(telegram_id=user_id, tier=3, status="Pending")
            session.add(req)
            await session.commit()
            req_id = req.id

        callback = self._create_callback_mock(f"approve_{req_id}")
        callback.from_user.id = 88888  # admin
        callback.message.edit_text = AsyncMock()
        callback.message.edit_caption = AsyncMock()
        
        bot = AsyncMock()
        invite_mock = MagicMock()
        invite_mock.invite_link = "https://t.me/test_link"
        bot.create_chat_invite_link.return_value = invite_mock

        # Mock is_admin
        import handlers.admin
        original_is_admin = handlers.admin.is_admin
        handlers.admin.is_admin = lambda uid: True

        try:
            # First approval
            await process_subscription_request(callback, bot)
            
            async with self.async_session() as session:
                req_obj = (await session.execute(select(SubscriptionRequest).where(SubscriptionRequest.id == req_id))).scalars().first()
                self.assertEqual(req_obj.status, "Approved")
                
                tickets = (await session.execute(select(Ticket).where(Ticket.user_id == user_id))).scalars().all()
                self.assertEqual(len(tickets), 3)

            # Reset mocks
            callback.answer.reset_mock()
            
            # Second approval attempt
            await process_subscription_request(callback, bot)
            
            # Verify callback answers that request is not found or already processed
            callback.answer.assert_called_once_with("Заявка не найдена или уже обработана.", show_alert=True)
            
            async with self.async_session() as session:
                tickets_after = (await session.execute(select(Ticket).where(Ticket.user_id == user_id))).scalars().all()
                # Should not have generated extra tickets
                self.assertEqual(len(tickets_after), 3)
        finally:
            handlers.admin.is_admin = original_is_admin

    # 6. Self-referral Ignored
    async def test_self_referral_ignored(self):
        user_id = 12345
        async with self.async_session() as session:
            # User referring themselves (ref_id = user_id)
            session.add(User(telegram_id=user_id, ref_id=user_id))
            req = SubscriptionRequest(telegram_id=user_id, tier=2, status="Pending")
            session.add(req)
            await session.commit()
            req_id = req.id

        callback = self._create_callback_mock(f"approve_{req_id}")
        callback.from_user.id = 88888  # admin
        callback.message.edit_text = AsyncMock()
        callback.message.edit_caption = AsyncMock()
        
        bot = AsyncMock()
        invite_mock = MagicMock()
        invite_mock.invite_link = "https://t.me/test_link"
        bot.create_chat_invite_link.return_value = invite_mock

        import handlers.admin
        original_is_admin = handlers.admin.is_admin
        handlers.admin.is_admin = lambda uid: True

        try:
            await process_subscription_request(callback, bot)
            
            async with self.async_session() as session:
                bonus_tickets = (await session.execute(select(Ticket).where(Ticket.user_id == user_id, Ticket.ticket_type == "bonus"))).scalars().all()
                # Should not get any bonus tickets
                self.assertEqual(len(bonus_tickets), 0)
        finally:
            handlers.admin.is_admin = original_is_admin

    # 7. Non-existent Referrer Ignored
    async def test_nonexistent_referrer_ignored(self):
        user_id = 12345
        async with self.async_session() as session:
            # User referring non-existent user 99999
            session.add(User(telegram_id=user_id, ref_id=99999))
            req = SubscriptionRequest(telegram_id=user_id, tier=2, status="Pending")
            session.add(req)
            await session.commit()
            req_id = req.id

        callback = self._create_callback_mock(f"approve_{req_id}")
        callback.from_user.id = 88888  # admin
        callback.message.edit_text = AsyncMock()
        callback.message.edit_caption = AsyncMock()
        
        bot = AsyncMock()
        invite_mock = MagicMock()
        invite_mock.invite_link = "https://t.me/test_link"
        bot.create_chat_invite_link.return_value = invite_mock

        import handlers.admin
        original_is_admin = handlers.admin.is_admin
        handlers.admin.is_admin = lambda uid: True

        try:
            # This should run without raising any exceptions
            await process_subscription_request(callback, bot)
            
            async with self.async_session() as session:
                friend_tickets = (await session.execute(select(Ticket).where(Ticket.user_id == user_id))).scalars().all()
                self.assertEqual(len(friend_tickets), 2)
        finally:
            handlers.admin.is_admin = original_is_admin

    # 8. Referral Double Reward Blocked
    async def test_referral_double_reward_blocked(self):
        referrer_id = 11111
        friend_id = 22222
        async with self.async_session() as session:
            session.add(User(telegram_id=referrer_id))
            # Friend already had referral_rewarded = True
            session.add(User(telegram_id=friend_id, ref_id=referrer_id, referral_rewarded=True))
            req = SubscriptionRequest(telegram_id=friend_id, tier=2, status="Pending")
            session.add(req)
            await session.commit()
            req_id = req.id

        callback = self._create_callback_mock(f"approve_{req_id}")
        callback.from_user.id = 88888  # admin
        callback.message.edit_text = AsyncMock()
        callback.message.edit_caption = AsyncMock()
        
        bot = AsyncMock()
        invite_mock = MagicMock()
        invite_mock.invite_link = "https://t.me/test_link"
        bot.create_chat_invite_link.return_value = invite_mock

        import handlers.admin
        original_is_admin = handlers.admin.is_admin
        handlers.admin.is_admin = lambda uid: True

        try:
            await process_subscription_request(callback, bot)
            
            async with self.async_session() as session:
                referrer_bonus_tickets = (await session.execute(
                    select(Ticket).where(Ticket.user_id == referrer_id, Ticket.ticket_type == "bonus")
                )).scalars().all()
                # Should be 0 since double reward is blocked
                self.assertEqual(len(referrer_bonus_tickets), 0)
        finally:
            handlers.admin.is_admin = original_is_admin

if __name__ == "__main__":
    unittest.main()
