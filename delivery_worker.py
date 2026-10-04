"""Durable notification outbox; Telegram sends happen after database commit."""
import asyncio
import logging
from config import client, DEVELOPER_ID
from database import db
from security import safe_send, RecipientUnavailableError
from styled_buttons import Button

async def deliver_job(job):
    payload = job['payload']; uid = payload['user_id']
    if await asyncio.to_thread(db.is_delivery_blocked, uid):
        raise RecipientUnavailableError('Recipient unavailable')
    if job['kind'] in ('content','scheduled'):
        content = await asyncio.to_thread(db.get_stage_content_by_id,payload['content_id'])
        profile = await asyncio.to_thread(db.get_user_stage,uid)
        if not content or not profile or profile[0] != content[1] or await asyncio.to_thread(db.is_banned,uid) or not await asyncio.to_thread(db.notifications_enabled,uid): return
        await safe_send(uid,f"📢 محتوى دراسي جديد\n📝 {content[8] or 'بدون وصف'}",buttons=[[Button.inline('عرض المحتوى',f'stage_content:view:{content[0]}')]],parse_mode=None)
    elif job['kind'] == 'support':
        ticket = await asyncio.to_thread(db.get_ticket_info,payload['ticket_id'])
        if not ticket: return
        if uid != DEVELOPER_ID and uid != await asyncio.to_thread(db.get_support_admin,ticket[2]): return
        await safe_send(uid,f'📩 تذكرة دعم #{payload["ticket_id"]}\n👤 {ticket[3]}\n🆔 {ticket[0]}\n\n{ticket[1]}',buttons=[[Button.inline('📩 الرد على التذكرة',f'support_reply:{payload["ticket_id"]}')]],parse_mode=None)
    elif job['kind'] == 'support_reply':
        ticket=await asyncio.to_thread(db.get_ticket_info,payload['ticket_id'])
        if not ticket:raise ValueError('Ticket not found')
        record=await asyncio.to_thread(db.get_ticket_record,payload['ticket_id'])
        if record['status']=='closed':return
        if record['status']!='replying' or record.get('reply')!=payload['reply'] or record.get('reply_by')!=payload['actor']:raise ValueError('Reply changed')
        actor=payload['actor']
        if actor!=DEVELOPER_ID and actor!=await asyncio.to_thread(db.get_support_admin,ticket[2]):raise PermissionError('Support scope revoked')
        if await asyncio.to_thread(db.is_banned,actor) or not await asyncio.to_thread(db.is_admin,actor):raise PermissionError('Support permission revoked')
        await safe_send(uid,f'📩 رد الدعم على تذكرتك #{payload["ticket_id"]}\n\n💬 سؤالك: {ticket[1][:1800]}\n\n✅ الرد: {payload["reply"]}',parse_mode=None)
        await asyncio.to_thread(db.close_support_ticket,payload['ticket_id'],actor,payload['reply'])
    elif job['kind'] == 'backup':
        import os
        from backup_tools import validate_manifest
        if not os.path.isfile(payload['path']):payload['path']=await asyncio.to_thread(db.create_backup)
        metadata=await asyncio.to_thread(validate_manifest,payload['path'])
        sent = await asyncio.wait_for(client.send_file(uid,payload['path'],caption='💾 نسخة احتياطية موثقة\nSHA256: '+metadata['sha256'],force_document=True),180)
        await asyncio.wait_for(client.send_file(uid,payload['path']+'.json',caption='بيانات التحقق من النسخة الاحتياطية',force_document=True),60)
        await asyncio.to_thread(db.confirm_backup_delivery,payload['path'],sent.id,uid)
    else:
        raise ValueError('Unknown delivery job')

async def delivery_loop():
    await asyncio.to_thread(db.recover_jobs)
    while True:
        job = await asyncio.to_thread(db.claim_job)
        if not job:
            await asyncio.sleep(1); continue
        try:
            await deliver_job(job)
            await asyncio.to_thread(db.finish_job,job['id'],'sent')
        except asyncio.CancelledError:
            # Retain sending: on restart it becomes uncertain for manual review.
            raise
        except Exception as error:
            error_name = type(error).__name__
            permanent = isinstance(error, RecipientUnavailableError) or error_name in ('UserIsBlockedError','InputUserDeactivatedError','PeerIdInvalidError')
            if error_name in ('UserIsBlockedError','InputUserDeactivatedError'):
                await asyncio.to_thread(db.set_delivery_blocked, job['payload']['user_id'], True, 'blocked' if error_name == 'UserIsBlockedError' else 'deactivated')
            uncertain = isinstance(error,(asyncio.TimeoutError,ConnectionError,OSError))
            state = 'uncertain' if uncertain else 'failed' if permanent or job['attempts']>=5 else 'pending'
            delay = max(min(3600,2**job['attempts']*15),getattr(error,'seconds',0)+1)
            await asyncio.to_thread(db.finish_job,job['id'],state,error_name,delay)
            logging.warning('Delivery %s became %s: %s',job['id'],state,error_name)
        await asyncio.sleep(0.1)
