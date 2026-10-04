"""Safe backup diagnostics and persistent Telegram delivery suppression."""
import asyncio
import subprocess
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
from test_regressions import db, Database, main, client, utils
import backup_tools
import security
from telethon.errors import UserIsBlockedError, InputUserDeactivatedError


class BackupDiagnosticsTests(unittest.TestCase):
    def test_failures_have_specific_safe_messages(self):
        cases = [
            ('aborting because of server version mismatch', 'version_mismatch'),
            ('password authentication failed for user secret-user', 'authentication'),
            ('no pg_hba.conf entry for host private-host', 'access_rule'),
            ('SSL error: certificate verify failed', 'ssl'),
            ('could not translate host name secret-host', 'dns'),
            ('connection refused', 'connection'),
            ('permission denied for table private-table', 'permission'),
            ('No space left on device', 'disk_full'),
            ('database secret-db does not exist', 'missing_database'),
            ('unknown failure password=top-secret postgresql://private/secret', 'unknown'),
        ]
        for stderr, code in cases:
            error = subprocess.CalledProcessError(1, ['pg_dump'], stderr=stderr+' password=top-secret postgresql://private/secret')
            with patch('backup_tools.subprocess.run', side_effect=error):
                with self.assertRaises(backup_tools.BackupToolError) as caught:
                    backup_tools.run_tool(['pg_dump'], {})
            self.assertEqual(caught.exception.code, code)
            for secret in ('top-secret', 'postgresql://', 'secret-user', 'secret-host', 'private-table', 'secret-db'):
                self.assertNotIn(secret, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_versions_accept_newer_client_and_reject_old_or_mixed_pair(self):
        for dump, restore, server, code in [(18,18,170004,None), (17,17,180001,'version_mismatch'), (18,17,170000,'restore_version')]:
            outputs = [types.SimpleNamespace(stdout=f'{tool} (PostgreSQL) {version}.1') for tool, version in [('pg_dump',dump),('pg_restore',restore)]]
            with patch('backup_tools.run_tool', side_effect=outputs):
                if code:
                    with self.assertRaises(backup_tools.BackupToolError) as caught: backup_tools.check_backup_versions(server, {})
                    self.assertEqual(caught.exception.code, code)
                else: self.assertEqual(backup_tools.check_backup_versions(server,{})['pg_dump'],dump)

    def test_unknown_version_is_not_logged_verbatim(self):
        with patch('backup_tools.run_tool', return_value=types.SimpleNamespace(stdout='password=secret')):
            with self.assertRaises(backup_tools.BackupToolError) as caught: backup_tools.check_backup_versions(170000,{})
        self.assertNotIn('secret', str(caught.exception))

    def test_missing_tool_and_timeout_are_safe(self):
        for error, code in [(FileNotFoundError('secret/path'), 'missing_tool'), (subprocess.TimeoutExpired('secret command', 15, stderr='private'), 'timeout')]:
            with patch('backup_tools.subprocess.run', side_effect=error):
                with self.assertRaises(backup_tools.BackupToolError) as caught: backup_tools.run_tool(['pg_dump'], {})
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn('secret', str(caught.exception))

    def test_preflight_rejects_before_dump_and_leaves_no_partial(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            instance=Database(); instance._backend='postgresql'; instance.database_url='host=db dbname=students user=bot'; instance.backup_path=folder
            try:
                with patch.object(instance,'get_postgres_server_version',return_value=180000), patch('database.shutil.which',return_value='tool'), patch('backup_tools.run_tool',side_effect=[types.SimpleNamespace(stdout='pg_dump (PostgreSQL) 17.1'),types.SimpleNamespace(stdout='pg_restore (PostgreSQL) 17.1')]) as tool:
                    with self.assertRaises(backup_tools.BackupToolError): instance.create_backup()
                    self.assertEqual(tool.call_count,2)
                    self.assertEqual(list(Path(folder).iterdir()),[])
            finally:instance.close()


class DeliveryResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=900000000+time.time_ns()%100000000
        db.add_user(self.uid,None,'Blocked','Student');db.set_user_stage(self.uid,'Blocked Student',4)
        client.send_message.reset_mock(); client.send_message.side_effect=None
    async def asyncTearDown(self): client.send_message.side_effect=None

    async def test_blocked_fact_not_consumed_and_no_repeat_sends(self):
        client.send_message.side_effect=UserIsBlockedError(request=None)
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(await main.deliver_physics_fact(self.uid,4,daily=True))
        self.assertNotIn('Traceback', '\n'.join(logs.output))
        self.assertTrue(db.is_delivery_blocked(self.uid))
        self.assertNotIn(self.uid,db.get_daily_physics_recipients())
        self.assertNotIn(self.uid,db.get_users_eligible_for_physics_info())
        before=client.send_message.await_count
        self.assertFalse(await main.deliver_physics_fact(self.uid,4,daily=True))
        self.assertEqual(client.send_message.await_count,before)
        reservation=db.reserve_physics_fact(self.uid)
        from physics_facts import fact_for_position
        self.assertEqual(reservation['fact']['id'],fact_for_position(self.uid,0)['id'])
        db.cancel_physics_fact(self.uid,reservation['token'])
        self.assertFalse(db.is_banned(self.uid))

    async def test_state_persists_and_preferences_do_not_erase_block(self):
        db.set_delivery_blocked(self.uid)
        self.assertFalse(db.toggle_notifications(self.uid))
        self.assertTrue(db.is_delivery_blocked(self.uid))
        other=Database()
        try:self.assertTrue(other.is_delivery_blocked(self.uid))
        finally:other.close()
        db.set_delivery_blocked(self.uid,False)
        self.assertFalse(db.notifications_enabled(self.uid))
        self.assertFalse(db.is_delivery_blocked(self.uid))

    async def test_new_private_event_restores_delivery_without_changing_preferences(self):
        db.set_delivery_blocked(self.uid)
        db.toggle_notifications(self.uid)
        event=types.SimpleNamespace(sender_id=self.uid,is_private=True,chat_id=self.uid,reply=AsyncMock())
        with patch.object(utils,'check_subscription',new=AsyncMock(return_value=True)):
            self.assertTrue(await security.guard_event(event))
        self.assertFalse(db.is_delivery_blocked(self.uid));self.assertFalse(db.notifications_enabled(self.uid))
        await security.safe_send(self.uid,'reply')
        client.send_message.assert_awaited_once()

    async def test_deleted_account_is_permanent_but_network_failure_is_not(self):
        client.send_message.side_effect=InputUserDeactivatedError(request=None)
        with self.assertRaises(security.RecipientUnavailableError):await security.safe_send(self.uid,'hello')
        self.assertTrue(db.is_delivery_blocked(self.uid))
        db.set_delivery_blocked(self.uid,False)
        client.send_message.side_effect=ConnectionError('Temporary network outage')
        with self.assertRaises(ConnectionError):await security.safe_send(self.uid,'hello')
        self.assertFalse(db.is_delivery_blocked(self.uid))

    async def test_daily_batch_continues_after_blocked_recipient(self):
        other_uid=self.uid+100000001
        db.add_user(other_uid,None,'Other','Student');db.set_user_stage(other_uid,'Other Student',4)
        async def send(uid,*args,**kwargs):
            if uid==self.uid:raise UserIsBlockedError(request=None)
            return types.SimpleNamespace(id=1)
        with patch.object(db,'get_daily_physics_recipients',return_value=[self.uid,other_uid]),patch.object(client,'send_message',new=AsyncMock(side_effect=send)),patch('main.asyncio.sleep',new=AsyncMock()),patch('main.logging.exception') as errors:
            await main.send_daily_physics_info()
            errors.assert_not_called()
        self.assertTrue(db.is_delivery_blocked(self.uid))
        self.assertNotIn(other_uid,db.get_daily_physics_recipients())

    async def test_backup_alert_is_specific_and_unknown_errors_hide_credentials(self):
        for error, expected in [(backup_tools.BackupToolError('authentication', 'رفض بيانات الدخول'), '[authentication]'), (ValueError('postgresql://user:password@private'), 'فشل إعداد')]:
            with patch.object(db,'get_due_scheduled_content',return_value=[]),patch.object(db,'create_backup',side_effect=error),patch.object(main,'safe_send',new=AsyncMock()) as alert,patch('main.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError())),self.assertLogs(level='WARNING') as logs:
                with self.assertRaises(asyncio.CancelledError):await main.schedule_learning_maintenance()
            message=alert.await_args.args[1]
            self.assertIn(expected,message)
            self.assertNotIn('postgresql://',message+' '.join(logs.output))
            self.assertNotIn('password',message+' '.join(logs.output))

    async def test_outbox_permanent_failure_is_not_retried(self):
        import delivery_worker
        job={'id':'blocked-test','kind':'support','attempts':1,'payload':{'user_id':self.uid}}
        with patch.object(db,'recover_jobs'),patch.object(db,'claim_job',return_value=job),patch.object(db,'finish_job') as finish,patch.object(delivery_worker,'deliver_job',new=AsyncMock(side_effect=security.RecipientUnavailableError('Recipient unavailable'))),patch('delivery_worker.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):await delivery_worker.delivery_loop()
        self.assertEqual(finish.call_args.args[1], 'failed')
        finish.assert_called_once()
