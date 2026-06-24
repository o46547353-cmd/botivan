import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "dummy_token")

# Читаем список ID админов через запятую
admin_ids_str = os.getenv("ADMIN_IDS", "0")
ADMIN_IDS = []
for id_str in admin_ids_str.split(","):
    try:
        ADMIN_IDS.append(int(id_str.strip()))
    except ValueError:
        pass

try:
    PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
except ValueError:
    PRIVATE_CHANNEL_ID = 0

try:
    REQUESTS_GROUP_ID = int(os.getenv("REQUESTS_GROUP_ID", "-1003751172603"))
except ValueError:
    REQUESTS_GROUP_ID = -1003751172603

PRIVATE_CHANNEL_LINK = os.getenv("PRIVATE_CHANNEL_LINK", "")
DATABASE_URL = "sqlite+aiosqlite:///bot.db"
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@admin")