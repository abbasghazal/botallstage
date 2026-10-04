"""Behavioral regressions for backup, migration, durable jobs and scope checks."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from datetime import datetime,timedelta
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from test_regressions import Database,db,main,webapp,utils,client,signed,IRAQ_TZ
import database
import backup_tools
from delivery_worker import deliver_job

class BackupTests(unittest.TestCase):
    def test_external_host_and_credentials_are_explicit_and_private(self):
        dsn="host=db.example port=6543 dbname=students user=bot password='a:b\\\\c' sslmode=require"
        with backup_tools.postgres_environment(dsn) as (env,name):
            self.assertEqual(name,'students');self.assertEqual(env['PGHOST'],'db.example');self.assertEqual(env['PGPORT'],'6543');self.assertEqual(env['PGSSLMODE'],'require')
            self.assertNotIn('PGPASSWORD',env)
            private=Path(env['PGPASSFILE']);self.assertEqual(private.stat().st_mode & 0o777,0o600)
            self.assertIn('students:bot:',private.read_text())
        self.assertFalse(private.exists())
    def test_missing_host_cannot_fall_back_to_local_socket(self):
        with self.assertRaises(ValueError):
            with backup_tools.postgres_environment('dbname=students user=bot'):pass
    def test_inherited_postgres_host_does_not_override_dsn(self):
        with patch.dict(os.environ,{'PGHOST':'wrong','PGDATABASE':'wrong','PGPASSWORD':'secret'}):
            with backup_tools.postgres_environment('host=correct dbname=students user=bot') as (env,name):
                self.assertEqual(env['PGHOST'],'correct');self.assertEqual(name,'students');self.assertNotIn('PGPASSWORD',env)
    def test_manifest_detects_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'backup.dump';path.write_bytes(b'PGDMPexample')
            backup_tools.write_manifest(path);backup_tools.validate_manifest(path)
            path.write_bytes(b'corrupted')
            with self.assertRaises(ValueError):backup_tools.validate_manifest(path)
    def test_unconfirmed_backups_are_not_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(database,'BACKUP_PATH',folder):instance=Database()
            try:
                for index in range(10):
                    path=Path(folder)/f'b{index}.dump';path.write_bytes(b'valid');backup_tools.write_manifest(path)
                instance._prune_backups('.dump')
                self.assertEqual(len(list(Path(folder).glob('*.dump'))),10)
            finally:instance.close()
    def test_sqlite_backup_can_be_restored_and_contains_row_data(self):
        path=db.create_backup()
        with sqlite3.connect(path) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertGreater(restored.execute('SELECT count(*) FROM subjects').fetchone()[0],0)
        backup_tools.validate_manifest(path)
    def test_pg_dump_uses_explicit_destination_and_validates_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            instance=Database();instance._backend='postgresql';instance.database_url='host=external dbname=students user=bot password=hidden';instance.backup_path=folder
            calls=[]
            def run(command,env,timeout=180):
                calls.append((command,env))
                if command[0]=='pg_dump':Path(command[command.index('--file')+1]).write_bytes(b'PGDMPtest')
                return types.SimpleNamespace(stdout='valid')
            try:
                with patch.object(instance, 'get_postgres_server_version', return_value=170000), patch('backup_tools.check_backup_versions'), patch('database.shutil.which',return_value='/usr/bin/tool'),patch('backup_tools.run_tool',side_effect=run):path=instance.create_backup()
                self.assertEqual(calls[0][0][1:3],['--dbname','students']);self.assertEqual(calls[0][1]['PGHOST'],'external')
                self.assertNotIn('hidden',' '.join(calls[0][0]));self.assertEqual(calls[1][0][0:2],['pg_restore','--list'])
                self.assertTrue(Path(path).exists());self.assertFalse(list(Path(folder).glob('*.partial')))
            finally:instance.close()
    def test_failed_dump_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            instance=Database();instance._backend='postgresql';instance.database_url='host=external dbname=students user=bot';instance.backup_path=folder
            def fail(command,env,timeout=180):
                Path(command[command.index('--file')+1]).write_bytes(b'partial');raise RuntimeError('failed')
            try:
                with patch.object(instance, 'get_postgres_server_version', return_value=170000), patch('backup_tools.check_backup_versions'), patch('database.shutil.which',return_value='tool'),patch('backup_tools.run_tool',side_effect=fail),self.assertRaises(RuntimeError):instance.create_backup()
                self.assertEqual(list(Path(folder).iterdir()),[])
            finally:instance.close()

class HardenedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.uid=400000000+time.time_ns()%10000000
        db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',1)
    def content(self):return db.add_stage_content(1,'mathematics',1,'text',None,text='audit content')
    def test_normal_student_can_view_own_content(self):
        self.assertEqual(db.get_stage_content_by_id(self.content())[1],1)
    def test_hot_writes_leave_legacy_snapshot_unchanged(self):
        before=db._execute('SELECT value FROM app_data WHERE key=?',('users',)).fetchone()[0]
        db.add_user(self.uid,None,'Changed','Name')
        after=db._execute('SELECT value FROM app_data WHERE key=?',('users',)).fetchone()[0]
        self.assertEqual(before,after);self.assertEqual(db._get_user_by_id(self.uid)['first_name'],'Changed')
    def test_support_reply_retains_ticket_and_history(self):
        tid=db.add_support_ticket(self.uid,'please help',1)
        db.queue_support_reply(tid,123,'response')
        ticket=next(x for x in db._read_data('support_tickets') if x['id']==tid)
        self.assertEqual(ticket['status'],'replying');self.assertEqual(ticket['reply'],'response')
        with self.assertRaises(ValueError):db.queue_support_reply(tid,123,'duplicate')
        db.close_support_ticket(tid,123,'response')
        self.assertIsNotNone(db.get_ticket_info(tid));self.assertFalse(any(x[0]==tid for x in db.get_support_tickets()))
        self.assertEqual(next(x for x in db._read_data('support_tickets') if x['id']==tid)['closed_by'],123)
    def test_support_notifications_created_once_with_ticket(self):
        tid=db.add_support_ticket(self.uid,'help me',1)
        rows=db._execute('SELECT id FROM delivery_jobs WHERE id=?',(f'support:{tid}:123',)).fetchall()
        self.assertEqual(len(rows),1)
        db.enqueue_job(f'support:{tid}:123','support',{})
        self.assertEqual(len(db._execute('SELECT id FROM delivery_jobs WHERE id=?',(f'support:{tid}:123',)).fetchall()),1)
    def test_wrong_stage_admin_cannot_upload_schedule_delete_or_moderate(self):
        db.add_admin(self.uid,None,'Scoped Admin',123)
        other=db.add_stage_content(2,'mathematics',1,'text',None,text='other stage')
        self.assertFalse(db.can_manage_stage(self.uid,2))
        with self.assertRaises(PermissionError):db.add_stage_content(2,'mathematics',1,'text',None,text='forbidden',added_by=self.uid)
        with self.assertRaises(PermissionError):db.delete_stage_content(other,self.uid)
        with self.assertRaises(PermissionError):db.schedule_content(other,(datetime.now(IRAQ_TZ)+timedelta(days=1)).isoformat(),self.uid)
        db.set_admin_stages(123,self.uid,[1,2]);self.assertTrue(db.can_manage_stage(self.uid,2))
    def test_content_commit_and_notification_queue_are_atomic(self):
        db.add_admin(self.uid,None,'Admin',123)
        db.set_workflow(self.uid,'upload',{'step':'content','stage':1,'subject_key':'mathematics','chapter_num':1,'content_type':'text'})
        payload=dict(stage=1,subject_key='mathematics',chapter_num=1,content_type='text',file_id=None,text='queued',added_by=self.uid)
        cid=db.finish_upload(self.uid,**payload)
        self.assertTrue(db._execute('SELECT 1 FROM delivery_jobs WHERE id=?',(f'content:{cid}:{self.uid}',)).fetchone())
        with self.assertRaises(PermissionError):db.finish_upload(self.uid,**payload)
    def test_failed_enqueue_rolls_back_saved_content(self):
        db.add_admin(self.uid,None,'Admin',123)
        db.set_workflow(self.uid,'upload',{'step':'content','stage':1,'subject_key':'mathematics','chapter_num':1,'content_type':'text'})
        before=db._execute('SELECT count(*) FROM content').fetchone()[0]
        with patch.object(db,'enqueue_job',side_effect=RuntimeError('queue unavailable')),self.assertRaises(RuntimeError):
            db.finish_upload(self.uid,stage=1,subject_key='mathematics',chapter_num=1,content_type='text',file_id=None,text='rollback',added_by=self.uid)
        self.assertEqual(db._execute('SELECT count(*) FROM content').fetchone()[0],before)
        self.assertTrue(db.get_workflow(self.uid,'upload'))
    def test_interrupted_job_requires_operator_review(self):
        jid=f'audit:{self.uid}';db.enqueue_job(jid,'content',{'user_id':self.uid,'content_id':self.content()})
        db.finish_job(jid,'sending');db.recover_jobs()
        self.assertEqual(db._execute('SELECT state FROM delivery_jobs WHERE id=?',(jid,)).fetchone()[0],'uncertain')
        with self.assertRaises(PermissionError):db.retry_job(jid,self.uid)
        self.assertTrue(db.retry_job(jid,123));self.assertEqual(db._execute('SELECT state FROM delivery_jobs WHERE id=?',(jid,)).fetchone()[0],'pending')
    def test_scheduled_notification_is_not_enqueued_twice(self):
        cid=self.content();sid=db.schedule_content(cid,(datetime.now(IRAQ_TZ)+timedelta(days=1)).isoformat(),123)
        self.assertTrue(db.queue_schedule(sid));self.assertFalse(db.queue_schedule(sid))
        self.assertEqual(len(db._execute('SELECT id FROM delivery_jobs WHERE id=?',(f'scheduled:{sid}:{self.uid}',)).fetchall()),1)
    def test_manual_fact_does_not_suppress_daily_delivery(self):
        fact=db.reserve_physics_fact(self.uid);db.complete_physics_fact(self.uid,fact['token'])
        self.assertIn(self.uid,db.get_daily_physics_recipients())
        db.mark_daily_physics(self.uid);self.assertNotIn(self.uid,db.get_daily_physics_recipients())
    def test_daily_fact_is_eligible_again_on_next_baghdad_date(self):
        db.mark_daily_physics(self.uid)
        db._execute('UPDATE daily_physics SET sent_date=? WHERE telegram_id=?',((datetime.now(IRAQ_TZ).date()-timedelta(days=1)).isoformat(),self.uid));db._connection.commit()
        self.assertIn(self.uid,db.get_daily_physics_recipients())
    def test_global_ai_budget_and_parallel_limit(self):
        with patch.dict(os.environ,{'OPENAI_GLOBAL_DAILY_LIMIT':'100','OPENAI_MAX_CONCURRENT':'1'}):
            # Isolate active test reservations left by other test modules.
            db._execute('DELETE FROM ai_reservations');db._execute('DELETE FROM ai_daily');db._connection.commit()
            token=db.reserve_ai(self.uid,80,1000);self.assertTrue(token)
            self.assertIsNone(db.reserve_ai(self.uid+1,1,1000))
            db.settle_ai(self.uid,token,80)
            self.assertIsNone(db.reserve_ai(self.uid+1,21,1000))
            self.assertTrue(db.reserve_ai(self.uid+1,20,1000))
    def test_legacy_data_migrates_and_sql_changes_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/legacy.db'
            with sqlite3.connect(path) as old:
                old.execute('CREATE TABLE app_data(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL)')
                old.execute('INSERT INTO app_data VALUES (?,?,?)',('users',json.dumps([{'user_id':7654321,'first_name':'Legacy','last_name':'Student','date_joined':'2026-01-01','last_active':'2026-01-01'}]),'2026-01-01'))
                old.execute('INSERT INTO app_data VALUES (?,?,?)',('users_stages',json.dumps([{'user_id':7654321,'full_name':'Legacy Student','stage':1,'date_registered':'2026-01-01'}]),'2026-01-01'))
            with patch.object(database,'DATABASE_PATH',path):
                one=Database();self.assertEqual(one.get_user_stage(7654321)[0],1)
                one._execute('UPDATE users SET first_name=? WHERE telegram_id=?',('Edited',7654321));one._connection.commit();one.close()
                two=Database();self.assertEqual(two._get_user_by_id(7654321)['first_name'],'Edited');self.assertFalse(two._execute('PRAGMA foreign_key_check').fetchall());two.close()

class HardenedAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=500000000+time.time_ns()%10000000;db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',1)
        client.send_message.reset_mock();utils._subscription_cache.clear()
    async def test_student_view_is_not_blocked_by_admin_scope(self):
        cid=db.add_stage_content(1,'mathematics',1,'text',None,text='visible')
        event=types.SimpleNamespace(sender_id=self.uid,chat_id=self.uid,answer=AsyncMock(),reply=AsyncMock())
        await main.handle_view_content(event,cid,False)
        self.assertGreaterEqual(client.send_message.await_count,1)
        self.assertFalse(any('خيارات الأدمن' in call.args[1] for call in client.send_message.await_args_list))
        self.assertEqual(db.get_user_progress(self.uid)['viewed'],1)
    async def test_upload_returns_without_waiting_for_subscribers(self):
        db.add_admin(self.uid,None,'Admin',123)
        await utils.content_upload_handler.start_upload_session(self.uid,1,'mathematics',1,'text')
        await utils.content_upload_handler.process_description(self.uid,'description')
        with patch.object(utils.ContentSender,'notify_new_stage_content',new=AsyncMock(side_effect=AssertionError('must not send inline'))),patch.object(client,'get_entity',create=True,new=AsyncMock(side_effect=AssertionError('must not lookup inline'))):
            result=await utils.content_upload_handler.process_content(self.uid,types.SimpleNamespace(raw_text='content',text='content'))
        self.assertTrue(result.startswith('✅'))
    async def test_web_support_ticket_has_durable_reply_button(self):
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_post('/api/support',webapp.support)
        async with TestClient(TestServer(app)) as http:
            response=await http.post('/api/support',headers={'X-Telegram-Init-Data':signed(self.uid)},json={'message':'support from website'})
            self.assertEqual(response.status,200);tid=(await response.json())['id']
        await deliver_job({'kind':'support','payload':{'ticket_id':tid,'user_id':123}})
        buttons=client.send_message.await_args.kwargs['buttons']
        self.assertEqual(buttons[0][0].type.data,f'support_reply:{tid}'.encode())
    async def test_support_reply_closes_only_after_delivery(self):
        tid=db.add_support_ticket(self.uid,'question',1);db.queue_support_reply(tid,123,'answer')
        await deliver_job({'kind':'support_reply','payload':{'ticket_id':tid,'user_id':self.uid,'actor':123,'reply':'answer'}})
        self.assertEqual(next(x for x in db._read_data('support_tickets') if x['id']==tid)['status'],'closed')
    async def test_subscription_cache_and_temporary_failure_are_distinct(self):
        with patch.object(db,'get_required_channels',return_value=[(1,42,'channel','title')]),patch.object(utils,'_is_subscribed_to_channel',new=AsyncMock(return_value=True)) as check:
            self.assertTrue(await utils.check_subscription(self.uid));self.assertTrue(await utils.check_subscription(self.uid));self.assertEqual(check.await_count,1)
        utils._subscription_cache.clear()
        with patch.object(db,'get_required_channels',return_value=[(1,42,'channel','title')]),patch.object(utils,'_is_subscribed_to_channel',new=AsyncMock(side_effect=utils.SubscriptionUnavailable('temporary'))):
            with self.assertRaises(utils.SubscriptionUnavailable):await utils.check_subscription(self.uid)
