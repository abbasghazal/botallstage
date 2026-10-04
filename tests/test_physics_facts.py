import asyncio
import json
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch
from test_regressions import Database, db, main, client
import database
from physics_facts import load_facts, fact_for_position, FACT_COUNT, format_fact

class PhysicsLibraryTests(unittest.TestCase):
    def test_exact_count_unique_and_categories(self):
        facts=load_facts()
        self.assertEqual(len(facts),1000)
        self.assertEqual(len({f['text'] for f in facts}),1000)
        self.assertEqual([f['id'] for f in facts],list(range(1,1001)))
        self.assertEqual(len({f['category'] for f in facts}),100)
        self.assertTrue(all(len(format_fact(f))<4096 for f in facts))
        with self.assertRaises(TypeError):facts[0]['text']='changed'
    def test_shuffle_covers_each_cycle_and_each_user(self):
        first=[fact_for_position(123,i)['id'] for i in range(1000)]
        second=[fact_for_position(123,i)['id'] for i in range(1000,2000)]
        self.assertEqual(set(first),set(range(1,1001)))
        self.assertEqual(set(second),set(first))
        self.assertNotEqual(first,second)
        self.assertNotEqual(first,[fact_for_position(456,i)['id'] for i in range(1000)])
    def test_representative_calculations(self):
        facts=load_facts()
        self.assertIn('8 جول',next(f['text'] for f in facts if f['category']=='طاقة الحركة'))
        self.assertIn('0.1 ثانية',next(f['text'] for f in facts if f['category']=='زمن دائرة RC'))
        self.assertIn('50%',next(f['text'] for f in facts if f['category']=='الاضمحلال الإشعاعي'))
        self.assertIn('720000 جول',next(f['text'] for f in facts if f['category']=='طاقة الكهرباء'))
    def test_no_channel_calls_in_active_handlers(self):
        import inspect
        for function in (main.deliver_physics_fact,main.handle_physics_info_stage,main.send_daily_physics_info):
            source=inspect.getsource(function)
            self.assertNotIn('iter_messages',source)
            self.assertNotIn('get_physics_info_channel',source)

class PhysicsStateTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.patcher=patch.object(database,'DATABASE_PATH',self.folder.name+'/physics.db');self.patcher.start()
        self.database=Database()
        self.database.add_user(111,None,'Test','User');self.database.set_user_stage(111,'Test User',1)
    def tearDown(self):
        self.database.close();self.patcher.stop();self.folder.cleanup()
    def test_all_thousand_then_restart(self):
        ids=[]
        for _ in range(1000):
            reserved=self.database.reserve_physics_fact(111);ids.append(reserved['fact']['id'])
            self.assertTrue(self.database.complete_physics_fact(111,reserved['token']))
        self.assertEqual(len(set(ids)),1000)
        other=Database()
        try:
            reserved=other.reserve_physics_fact(111)
            self.assertEqual(reserved['fact']['id'],fact_for_position(111,1000)['id'])
            self.assertTrue(other.complete_physics_fact(111,reserved['token']))
            self.assertEqual(other._execute('SELECT position FROM physics_fact_state WHERE telegram_id=?',(111,)).fetchone()[0],1001)
        finally:other.close()
    def test_failed_send_retries_same_and_stale_token_cannot_advance(self):
        first=self.database.reserve_physics_fact(111)
        self.assertIsNone(self.database.reserve_physics_fact(111))
        self.database.cancel_physics_fact(111,first['token'])
        retry=self.database.reserve_physics_fact(111)
        self.assertEqual(first['fact'],retry['fact'])
        self.assertFalse(self.database.complete_physics_fact(111,first['token']))
        self.assertTrue(self.database.complete_physics_fact(111,retry['token']))
        self.assertFalse(self.database.complete_physics_fact(111,retry['token']))
    def test_expired_reservation_can_be_retried(self):
        reserved=self.database.reserve_physics_fact(111)
        self.database._execute('UPDATE physics_fact_state SET pending_until=0 WHERE telegram_id=?',(111,));self.database._connection.commit()
        retry=self.database.reserve_physics_fact(111)
        self.assertEqual(reserved['fact'],retry['fact'])
        self.database.cancel_physics_fact(111,reserved['token'])
        self.assertIsNone(self.database.reserve_physics_fact(111))
    def test_concurrent_reservations_across_connections(self):
        other=Database()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(d.reserve_physics_fact,111) for d in (self.database,other)]
                results=[f.result() for f in futures]
            self.assertEqual(sum(r is not None for r in results),1)
        finally:other.close()
    def test_daily_eligibility_and_preferences(self):
        self.assertIn(111,self.database.get_users_eligible_for_physics_info())
        r=self.database.reserve_physics_fact(111);self.database.complete_physics_fact(111,r['token'])
        self.assertNotIn(111,self.database.get_users_eligible_for_physics_info())
        self.database._execute('UPDATE physics_fact_state SET last_sent=? WHERE telegram_id=?',(time.time()-90000,111));self.database._connection.commit()
        self.assertIn(111,self.database.get_users_eligible_for_physics_info())
        self.database.toggle_notifications(111)
        self.assertNotIn(111,self.database.get_users_eligible_for_physics_info())

    def test_banned_user_is_excluded_from_daily(self):
        self.database.ban_user(111)
        self.assertNotIn(111,self.database.get_users_eligible_for_physics_info())

class PhysicsBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=90000000+int(time.time_ns()%1000000)
        db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',4)
        client.send_message.reset_mock()
    def event(self):
        return types.SimpleNamespace(sender_id=self.uid,chat_id=self.uid,is_private=True,
                                     answer=AsyncMock(),reply=AsyncMock(),edit=AsyncMock())
    async def test_manual_fact_and_buttons_without_channel(self):
        e=self.event()
        with patch.object(db,'get_physics_info_channel',side_effect=AssertionError('channel read')):
            await main.handle_physics_info_stage(e,4)
        self.assertEqual(client.send_message.await_count,1)
        args=client.send_message.await_args
        self.assertEqual(args.args[0],self.uid)
        self.assertIn('معلومة فيزيائية',args.args[1])
        self.assertIsNone(args.kwargs['parse_mode'])
        callbacks=[b.type.data for row in args.kwargs['buttons'] for b in row]
        self.assertIn(b'stage_4:physics_info',callbacks);self.assertIn(b'stage_4:home',callbacks)
    async def test_daily_uses_same_history(self):
        await main.deliver_physics_fact(self.uid,4)
        first=client.send_message.await_args.args[1]
        with patch.object(db,'get_users_eligible_for_physics_info',return_value=[self.uid]):
            await main.send_daily_physics_info()
        second=client.send_message.await_args.args[1]
        self.assertIn('يومية',second)
        self.assertNotEqual(first.split('\n\n')[1],second.split('\n\n')[1])
        self.assertEqual(db._execute('SELECT position FROM physics_fact_state WHERE telegram_id=?',(self.uid,)).fetchone()[0],2)
    async def test_send_failure_releases_and_preserves_position(self):
        with patch.object(main,'safe_send',new=AsyncMock(side_effect=RuntimeError('test failed send'))):
            with self.assertRaises(RuntimeError):await main.deliver_physics_fact(self.uid,4)
        reservation=db.reserve_physics_fact(self.uid)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation['fact']['id'],fact_for_position(self.uid,0)['id'])
        db.cancel_physics_fact(self.uid,reservation['token'])
    async def test_cancellation_releases_reservation(self):
        with patch.object(main,'safe_send',new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):await main.deliver_physics_fact(self.uid,4)
        retry=db.reserve_physics_fact(self.uid);self.assertIsNotNone(retry)
        db.cancel_physics_fact(self.uid,retry['token'])
    async def test_rate_limit(self):
        e=self.event()
        with patch.object(main,'action_limit',new=AsyncMock(return_value=False)) as limited:
            await main.handle_physics_info_stage(e,4)
        limited.assert_awaited_once_with(self.uid,'physics_info',6,60)
        client.send_message.assert_not_awaited();self.assertTrue(e.answer.await_args.kwargs['alert'])
    async def test_invalid_stage_does_not_send(self):
        await main.handle_physics_info_stage(self.event(),2)
        client.send_message.assert_not_awaited()
