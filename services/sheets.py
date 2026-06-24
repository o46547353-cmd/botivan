import asyncio
import logging
import os
from sqlalchemy import select, func
from database.db import async_session
from database.models import User, Ticket

async def sync_user_data(telegram_id: int):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        creds_path = "credentials.json"
        spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        
        if not os.path.exists(creds_path) or not spreadsheet_id:
            return

        async with async_session() as session:
            user_res = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = user_res.scalars().first()
            if not user:
                return
            
            tickets_res = await session.execute(
                select(func.count()).select_from(Ticket).where(Ticket.user_id == telegram_id, Ticket.status == "Active")
            )
            tickets_count = tickets_res.scalar() or 0
            
            refs_res = await session.execute(
                select(func.count()).select_from(User).where(User.ref_id == telegram_id, User.referral_rewarded == True)
            )
            refs_count = refs_res.scalar() or 0

        def _sync_worker():
            scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(spreadsheet_id).get_worksheet(0)
            
            try:
                cell = sheet.find(str(telegram_id), in_column=1)
            except gspread.exceptions.CellNotFound:
                cell = None
                
            row_data = [
                str(telegram_id),
                user.subscription_status,
                str(user.tier),
                user.expire_date.strftime('%Y-%m-%d') if user.expire_date else '—',
                str(tickets_count),
                str(refs_count),
                user.phone_number or '—'
            ]
            
            if cell:
                sheet.update(range_name=f"A{cell.row}:G{cell.row}", values=[row_data])
            else:
                sheet.append_row(row_data)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_worker)
        logging.info(f"Google Sheets synchronized for user {telegram_id}")
        
    except ImportError:
        pass
    except Exception as e:
        logging.error(f"Error syncing user {telegram_id} to Google Sheets: {e}")