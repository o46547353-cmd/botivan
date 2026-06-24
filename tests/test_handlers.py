import unittest
import os
import sys

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from database.db import Base
from database.models import User, Ticket, BotConfig
from handlers.user import should_show_free_button, should_show_buy_button, main_menu_keyboard, free_tier_success_keyboard, clean_phone_number

class TestUserHandlers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create an in-memory async SQLite database for testing
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            
    async def asyncTearDown(self):
        await self.engine.dispose()
        
    async def test_should_show_buy_button_default(self):
        async with self.async_session() as session:
            # By default (no config in DB), should return True
            res = await should_show_buy_button(session)
            self.assertTrue(res)
            
    async def test_should_show_buy_button_disabled(self):
        async with self.async_session() as session:
            # Insert key = "buy_ticket_enabled", value = "false"
            cfg = BotConfig(key="buy_ticket_enabled", value="false")
            session.add(cfg)
            await session.commit()
            
            res = await should_show_buy_button(session)
            self.assertFalse(res)

    async def test_should_show_buy_button_enabled(self):
        async with self.async_session() as session:
            # Insert key = "buy_ticket_enabled", value = "true"
            cfg = BotConfig(key="buy_ticket_enabled", value="true")
            session.add(cfg)
            await session.commit()
            
            res = await should_show_buy_button(session)
            self.assertTrue(res)

    async def test_should_show_free_button_conditions(self):
        async with self.async_session() as session:
            user_id = 12345
            # Since free tickets are permanently hidden, should always return False
            res = await should_show_free_button(session, user_id)
            self.assertFalse(res)

    def test_main_menu_keyboard(self):
        # Test with buy and free button enabled
        markup = main_menu_keyboard(show_free=True, show_buy=True)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("menu_free_ticket", buttons)
        self.assertIn("menu_subscribe", buttons)
        
        # Test with buy button disabled, free button enabled
        markup = main_menu_keyboard(show_free=True, show_buy=False)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("menu_free_ticket", buttons)
        self.assertNotIn("menu_subscribe", buttons)
        
        # Test with both disabled
        markup = main_menu_keyboard(show_free=False, show_buy=False)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertNotIn("menu_free_ticket", buttons)
        self.assertNotIn("menu_subscribe", buttons)

    def test_free_tier_success_keyboard(self):
        # Test with show_buy = True
        markup = free_tier_success_keyboard("https://t.me/test", show_buy=True)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("menu_subscribe_force", buttons)
        
        # Test with show_buy = False
        markup = free_tier_success_keyboard("https://t.me/test", show_buy=False)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]
        self.assertNotIn("menu_subscribe_force", buttons)

    def test_clean_phone_number(self):
        self.assertEqual(clean_phone_number("+7 999 555 22 22"), "+79995552222")
        self.assertEqual(clean_phone_number("89995552222"), "+79995552222")
        self.assertEqual(clean_phone_number("79995552222"), "+79995552222")
        self.assertEqual(clean_phone_number("9995552222"), "+79995552222")
        self.assertEqual(clean_phone_number("8 (999) 555-22-22"), "+79995552222")
        self.assertEqual(clean_phone_number("+12345678901"), "+12345678901")
        self.assertIsNone(clean_phone_number("not-a-number"))

if __name__ == "__main__":
    unittest.main()
