"""Developer-only reporting with distinct Telegram reasons and lifecycle history."""
import asyncio
import json
import time
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from test_regressions import db, Database, main, client, utils
from telethon.errors import UserIsBlockedError, InputUserDeactivatedError
from telethon.tl.types import UpdateBotStopped
import security


class ReportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.uid=1200000000+time.time_ns()%100000000
        db.add_user(self.uid,'blocked_student','Known','User');db.set_user_stage(self.uid,'Student Name',4)
    def item(self, category):
        return next(x for x in db.get_bot_block_report(123,category,0,10)['items'] if x['user_id']==self.uid)

    def test_permissions_and_distinct_reasons(self):
        db.set_delivery_blocked(self.uid,True,'blocked')
        self.assertEqual(self.item('blocked')['username'],'blocked_student')
        self.assertEqual(self.item('blocked')['name'],'Student Name')
        db.add_admin(self.uid,None,'Admin',123)
        with self.assertRaises(PermissionError):db.get_bot_block_report(self.uid)
        db.set_delivery_blocked(self.uid,True,'deactivated')
        self.assertEqual(self.item('deactivated')['reason'],'deactivated')
        self.assertFalse(any(x['user_id']==self.uid for x in db.get_bot_block_report(123,'blocked')['items']))
        self.assertFalse(db.is_banned(self.uid))

    def test_returned_history_persistence_and_preferences(self):
        db.toggle_notifications(self.uid)
        db.set_delivery_blocked(self.uid,True,'blocked')
        detected=self.item('blocked')['detected_at']
        self.assertFalse(db.set_delivery_blocked(self.uid,True,'blocked'))
        self.assertEqual(self.item('blocked')['detected_at'],detected)
        db.set_delivery_blocked(self.uid,False)
        returned=self.item('returned')
        self.assertEqual(returned['reason'],'blocked')
        self.assertEqual(returned['detected_at'],detected)
        self.assertTrue(returned['returned_at'])
        self.assertFalse(db.notifications_enabled(self.uid))
        other=Database()
        try:self.assertTrue(any(x['user_id']==self.uid for x in other.get_bot_block_report(123,'returned')['items']))
        finally:other.close()

    def test_legacy_unknown_not_mislabeled_and_old_time_preserved(self):
        old='2026-09-01T12:00:00+03:00'
        db._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,0,?)',('notification_preferences',str(self.uid),json.dumps({'delivery_blocked':True,'delivery_changed_at':old,'enabled':False})))
        self.assertEqual(self.item('unavailable')['detected_at'],old)
        db.set_delivery_blocked(self.uid,False)
        item=self.item('returned')
        self.assertEqual(item['detected_at'],old)
        self.assertEqual(item['reason'],'unavailable')

    def test_pagination_and_unknown_profile(self):
        unknown=self.uid+10000000
        db.set_delivery_blocked(unknown,True,'blocked')
        rows=db.get_bot_block_report(123,'blocked',0,10)['items']
        row=next(x for x in rows if x['user_id']==unknown)
        self.assertEqual(row['name'],'اسم غير مسجل');self.assertIsNone(row['stage'])
        for i in range(12):db.set_delivery_blocked(self.uid+20000000+i,True,'deactivated')
        first=db.get_bot_block_report(123,'deactivated',0,8);second=db.get_bot_block_report(123,'deactivated',1,8)
        self.assertEqual(len(first['items']),8)
        self.assertFalse({x['user_id'] for x in first['items']}&{x['user_id'] for x in second['items']})
        final=db.get_bot_block_report(123,'deactivated',999999,8)
        self.assertEqual(final['page'],final['pages']-1)

    def test_out_of_order_native_events_do_not_revert_state(self):
        db.set_delivery_blocked(self.uid,True,'blocked',100)
        db.set_delivery_blocked(self.uid,True,'blocked',200)
        self.assertFalse(db.set_delivery_blocked(self.uid,False,'blocked',150))
        self.assertTrue(db.is_delivery_blocked(self.uid))
        db.set_delivery_blocked(self.uid,False,'blocked',300)
        self.assertFalse(db.set_delivery_blocked(self.uid,True,'blocked',250))
        self.assertFalse(db.is_delivery_blocked(self.uid))


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=1600000000+time.time_ns()%100000000
        db.add_user(self.uid,'known_user','Known','User');db.set_user_stage(self.uid,'Known Student',4)
        client.send_message.reset_mock();client.send_message.side_effect=None
    async def asyncTearDown(self):client.send_message.side_effect=None
    def event(self,uid=123,text='/blocked_users'):
        return types.SimpleNamespace(sender_id=uid,chat_id=uid,is_private=True,raw_text=text,reply=AsyncMock(),edit=AsyncMock(),answer=AsyncMock())

    async def test_send_errors_have_separate_report_categories(self):
        for error,reason in [(UserIsBlockedError(request=None),'blocked'),(InputUserDeactivatedError(request=None),'deactivated')]:
            db.set_delivery_blocked(self.uid,False)
            client.send_message.side_effect=error
            with self.assertRaises(security.RecipientUnavailableError):await security.safe_send(self.uid,'message')
            self.assertEqual(db.get_notification_preferences(self.uid)['delivery_block_reason'],reason)

    async def test_native_stop_start_and_timestamp(self):
        date=datetime.now(timezone.utc)
        await main.bot_stopped_update(UpdateBotStopped(self.uid,date,True,1))
        row=db.get_notification_preferences(self.uid)
        self.assertTrue(row['delivery_blocked']);self.assertEqual(row['delivery_block_reason'],'blocked')
        self.assertTrue(row['delivery_action_at'])
        await main.bot_stopped_update(UpdateBotStopped(self.uid,date,False,2))
        self.assertFalse(db.is_delivery_blocked(self.uid))
        self.assertEqual(db.get_notification_preferences(self.uid)['delivery_last_block_reason'],'blocked')

    async def test_duplicate_stop_in_same_second_cannot_override_restart(self):
        date=datetime.now(timezone.utc)
        stopped=UpdateBotStopped(self.uid,date,True,10)
        await main.bot_stopped_update(stopped)
        await main.bot_stopped_update(UpdateBotStopped(self.uid,date,False,11))
        await main.bot_stopped_update(stopped)
        self.assertFalse(db.is_delivery_blocked(self.uid))

    async def test_developer_menu_content_and_denied_admin(self):
        db.set_delivery_blocked(self.uid,True,'blocked')
        e=self.event();await main.handle_admin_management(e,'bot_blocks',['blocked','0'],True)
        text=e.edit.await_args.args[0]
        self.assertIn(str(self.uid),text);self.assertIn('@known_user',text);self.assertIn('Known Student',text)
        self.assertEqual(e.edit.await_args.kwargs['parse_mode'],None)
        other=self.event(self.uid)
        await main.handle_admin_management(other,'bot_blocks',['blocked','0'],True)
        other.edit.assert_not_awaited();other.answer.assert_awaited_once()
        from keyboards import Keyboards
        self.assertTrue(any(b.type.data==b'admin:bot_blocks:blocked:0' for row in Keyboards.admin_management_menu() for b in row))

    async def test_command_and_private_only_access(self):
        e=self.event()
        with patch.object(main,'guard_event',new=AsyncMock(return_value=True)):
            await main.operator_delivery_command(e)
        e.reply.assert_awaited_once()
        group=self.event();group.is_private=False
        await main.show_bot_blocks(group)
        group.edit.assert_not_awaited()
        self.assertIn('الخاصة',group.reply.await_args.args[0])
        other=self.event(self.uid)
        await main.operator_delivery_command(other)
        other.reply.assert_not_awaited()
