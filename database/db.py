import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import text, event
from sqlalchemy.pool import NullPool

from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, poolclass=NullPool, echo=False)

@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

Base = declarative_base()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN phone_number VARCHAR"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN referral_rewarded BOOLEAN DEFAULT FALSE"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
        except Exception:
            pass

async def cleanup_and_align_tickets():
    from database.models import Ticket
    from sqlalchemy import delete, select
    async with async_session() as session:
        # Delete inactive/annulled tickets
        await session.execute(delete(Ticket).where(Ticket.status != "Active"))
        
        # Select remaining active tickets ordered by id
        res = await session.execute(select(Ticket).order_by(Ticket.id))
        active_tickets = res.scalars().all()
        
        # Assign temporary numbers first to avoid unique constraint violations
        for idx, ticket in enumerate(active_tickets):
            ticket.ticket_number = f"temp_{idx + 1}"
        await session.commit()
        
        # Assign final sequential numbers
        for idx, ticket in enumerate(active_tickets):
            ticket.ticket_number = str(idx + 1)
        await session.commit()