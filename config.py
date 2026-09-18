from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio
import os
from typing import Tuple, Optional
from dotenv import load_dotenv

load_dotenv()

ENVIRONMENT = os.environ.get('ENVIRONMENT', os.environ.get('RENDER', '') and 'production' or 'development').strip().lower()

def _get_int_env(name, default=0):
    value = os.environ.get(name, str(default)).strip()
    try:
        return int(value) if value else default
    except ValueError:
        # A malformed optional setting must not prevent the bot from starting.
        return default

API_ID = _get_int_env('API_ID')
API_HASH = os.environ.get('API_HASH', '').strip()
BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()

DEVELOPER_ID = _get_int_env('DEVELOPER_ID', 6848908141)
# Kept as public configuration compatibility: deployment/admin tooling may
# populate it even when the current bot flow resolves administrators from DB.
ADMINS = [int(value) for value in os.environ.get('ADMINS', str(DEVELOPER_ID)).split(',') if value.strip()]
SESSION1 = os.environ.get('SESSION1', '').strip()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

DATA_PATH = os.environ.get('DATA_PATH', '.').strip() or '.'
os.makedirs(DATA_PATH, exist_ok=True)
DATABASE_PATH = os.environ.get('DATABASE_PATH', '').strip() or os.path.join(DATA_PATH, 'bot.db')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if ENVIRONMENT in {'production', 'prod'} and not DATABASE_URL:
    raise RuntimeError('DATABASE_URL must be configured in production; SQLite is development-only.')
BACKUP_PATH = os.environ.get('BACKUP_PATH', '').strip() or os.path.join(DATA_PATH, 'backups')
STORAGE_CHANNEL_ID = _get_int_env('STORAGE_CHANNEL_ID')
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
MINI_APP_URL = os.environ.get('MINI_APP_URL', '').strip() or (f'https://{RENDER_EXTERNAL_HOSTNAME}' if RENDER_EXTERNAL_HOSTNAME else '')
MINI_APP_PORT = _get_int_env('PORT', _get_int_env('MINI_APP_PORT', 8080))

DATA_KEYS = (
    'users', 'users_stages', 'subjects', 'admins', 'required_channels',
    'comments', 'comments_meta', 'search_channels', 'support_tickets',
    'ai_usage', 'settings', 'stage_content', 'physics_channel',
    'physics_requests', 'favorites', 'progress', 'quizzes', 'quiz_attempts',
    'notification_preferences', 'scheduled_content', 'audit_log'
)

def _get_event_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

def create_clients() -> Tuple[TelegramClient, Optional[TelegramClient], asyncio.AbstractEventLoop]:
    """Create and start Telethon clients (bot and optional user client).

    Returns (bot_client, user_client_or_None, loop)
    """
    loop = _get_event_loop()

    if not API_ID or not API_HASH:
        raise RuntimeError('API_ID and API_HASH must be set in environment')

    bot_client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

    user_client = None
    if SESSION1:
        try:
            user_client = TelegramClient(StringSession(SESSION1), API_ID, API_HASH, loop=loop)
        except Exception:
            user_client = None

    return bot_client, user_client, loop

bot_client, user_client, loop = create_clients()

# Public aliases retained for integrations and admin-side maintenance tools.
BOT_CLIENT = bot_client
USER_CLIENT = user_client
client = bot_client
