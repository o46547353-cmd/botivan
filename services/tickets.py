import random
from database.models import Ticket, BotConfig
from sqlalchemy import select, func

async def generate_tickets(session, user_id: int, count: int, ticket_type: str = "regular"):
    count_res = await session.execute(select(func.count()).select_from(Ticket))
    current_total = count_res.scalar() or 0
    
    for i in range(count):
        ticket_number = str(current_total + 1 + i)
        ticket = Ticket(
            user_id=user_id,
            ticket_number=ticket_number,
            status="Active",
            ticket_type=ticket_type
        )
        session.add(ticket)

async def get_tickets_counter_text(session) -> str:
    import config
    limit_res = await session.execute(select(BotConfig).where(BotConfig.key == "tickets_limit"))
    limit_cfg = limit_res.scalars().first()
    
    limit_val = None
    if limit_cfg and limit_cfg.value and limit_cfg.value.isdigit():
        limit_val = int(limit_cfg.value)
        
    tickets_count_res = await session.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "Active"))
    total_active_tickets = tickets_count_res.scalar() or 0
    
    channel_username = "@podarki_skynet"
    if config.PRIVATE_CHANNEL_LINK:
        if "/" in config.PRIVATE_CHANNEL_LINK:
            last_part = config.PRIVATE_CHANNEL_LINK.split("/")[-1]
            if not last_part.startswith("+") and last_part:
                channel_username = f"@{last_part}"
        else:
            channel_username = config.PRIVATE_CHANNEL_LINK
            if not channel_username.startswith("@"):
                channel_username = f"@{channel_username}"

    if limit_val is not None:
        counter_text = (
            f"📄 Всего купонов: {total_active_tickets}/{limit_val} шт\n"
            f"Канал розыгрыша: {channel_username}"
        )
    else:
        counter_text = (
            f"📄 Всего купонов: {total_active_tickets} шт (Без лимита)\n"
            f"Канал розыгрыша: {channel_username}"
        )
        
    return counter_text

async def get_tier_tickets(session, tier: int) -> int:
    return tier

async def get_all_tier_tickets(session) -> dict:
    defaults = {1: 1, 2: 1, 3: 5, 4: 10, 5: 100}
    res = {}
    for tier in range(1, 6):
        stmt = select(BotConfig).where(BotConfig.key == f"tickets_tier_{tier}")
        db_res = await session.execute(stmt)
        cfg = db_res.scalars().first()
        if cfg and cfg.value is not None and cfg.value.isdigit():
            res[tier] = int(cfg.value)
        else:
            res[tier] = defaults[tier]
    return res

def get_ticket_plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "билет"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return "билета"
    else:
        return "билетов"

def get_cert_plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "сертификат"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return "сертификата"
    else:
        return "сертификатов"

def get_coupon_plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "купон"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return "купона"
    else:
        return "купонов"