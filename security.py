"""Shared access policy for bot and Mini App."""
import asyncio
import time
from config import client, DEVELOPER_ID
from database import db

class RecipientUnavailableError(RuntimeError):
    pass

async def action_limit(uid,bucket,count=30,period=60):
    return await asyncio.to_thread(db.rate_limit,uid,bucket,count,period)

async def guard_event(event):
    if not getattr(event,'is_private',True):return False
    uid=event.sender_id
    if not uid or await asyncio.to_thread(db.is_banned,uid):return False
    # A new private message/callback proves this user can reach the bot again.
    if await asyncio.to_thread(db.is_delivery_blocked, uid):
        await asyncio.to_thread(db.set_delivery_blocked, uid, False)
    if not await action_limit(uid,'bot',60,60):return False
    from utils import check_subscription,send_subscription_message
    from utils import SubscriptionUnavailable
    try:
        subscribed = await check_subscription(uid)
    except SubscriptionUnavailable:
        await event.reply('تعذر التحقق من الاشتراك مؤقتاً، حاول بعد قليل.'); return False
    if not subscribed:
        await send_subscription_message(event.chat_id);return False
    await asyncio.to_thread(db.update_user_activity,uid)
    return True

async def content_access(event,cid):
    content=await asyncio.to_thread(db.get_stage_content_by_id,cid)
    stage=await asyncio.to_thread(db.get_user_stage,event.sender_id)
    allowed=content and (await asyncio.to_thread(db.can_manage_stage,event.sender_id,content[1]) or (stage and stage[0]==content[1]))
    if not allowed:
        try:await event.answer('المحتوى غير متاح.',alert=True)
        except Exception:await event.reply('المحتوى غير متاح.')
    return bool(allowed)

async def safe_send(uid,message,**kwargs):
    from telethon.errors import FloodWaitError, UserIsBlockedError, InputUserDeactivatedError
    if await asyncio.to_thread(db.is_delivery_blocked, uid):
        raise RecipientUnavailableError('Recipient unavailable')
    for attempt in range(3):
        try:return await asyncio.wait_for(client.send_message(uid,message,**kwargs),30)
        except (UserIsBlockedError, InputUserDeactivatedError) as error:
            reason = 'blocked' if isinstance(error, UserIsBlockedError) else 'deactivated'
            await asyncio.to_thread(db.set_delivery_blocked, uid, True, reason)
            raise RecipientUnavailableError('Recipient unavailable') from None
        except FloodWaitError as error:
            if error.seconds>60:raise
            await asyncio.sleep(error.seconds+1)
    raise RuntimeError('Send retry limit exceeded')
