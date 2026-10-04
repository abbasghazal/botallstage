import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
from aiohttp.test_utils import TestClient,TestServer
from test_regressions import main,webapp,db,client

class WorkerHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks=[]
    async def asyncTearDown(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        client.background_tasks=[]
        for key in ('runtime_phase','startup_deadline'):
            if hasattr(client,key):delattr(client,key)
    async def test_wait_retries_instead_of_crashing(self):
        with patch.object(db,'claim_worker',side_effect=[False,False,True]) as claim:
            await main.wait_for_worker(timeout=1,poll_interval=0.001)
        self.assertEqual(claim.call_count,3)
    async def test_busy_worker_has_bounded_wait(self):
        with patch.object(db,'claim_worker',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Timed out waiting'):
                await main.wait_for_worker(timeout=0.01,poll_interval=0.001)
    async def test_standby_health_allows_handover_but_not_ready(self):
        client.runtime_phase='waiting_worker';client.startup_deadline=time.monotonic()+60
        with patch.object(db,'ping',return_value=True),patch.object(client,'is_connected',return_value=False):
            health=await webapp.health(None);ready=await webapp.ready(None)
        self.assertEqual(health.status,200);self.assertEqual(ready.status,503)
        self.assertFalse(json.loads(health.text)['worker_ready'])
        self.assertEqual(json.loads(health.text)['phase'],'waiting_worker')
    async def test_expired_standby_and_database_failure_are_unhealthy(self):
        client.runtime_phase='waiting_worker';client.startup_deadline=time.monotonic()-1
        self.assertEqual((await webapp.health(None)).status,503)
        client.startup_deadline=time.monotonic()+60
        with patch.object(db,'ping',return_value=False):
            self.assertEqual((await webapp.health(None)).status,503)
    async def test_running_health_requires_live_schedulers(self):
        client.runtime_phase='running'
        db.claim_worker()
        self.tasks=[asyncio.create_task(asyncio.sleep(60))];client.background_tasks=self.tasks
        self.assertEqual((await webapp.ready(None)).status,200)
        self.tasks[0].cancel();await asyncio.gather(*self.tasks,return_exceptions=True)
        self.assertEqual((await webapp.health(None)).status,503)
    async def test_api_during_standby_returns_retry(self):
        client.runtime_phase='waiting_worker';handler=AsyncMock()
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_get('/api/me',handler)
        async with TestClient(TestServer(app)) as http:
            response=await http.get('/api/me')
            self.assertEqual(response.status,503);self.assertEqual(response.headers['Retry-After'],'5')
        handler.assert_not_awaited()
    async def test_config_rejects_invalid_timeout(self):
        for value in ('bad','0','-1','nan','3601'):
            with patch.dict(os.environ,{'WORKER_HANDOVER_TIMEOUT':value}):
                with self.assertRaises(ValueError):main.worker_handover_timeout()
        with patch.dict(os.environ,{'WORKER_HANDOVER_TIMEOUT':'300'}):
            self.assertEqual(main.worker_handover_timeout(),300)
    async def test_startup_order_and_lock_released_after_disconnect(self):
        calls=[]
        async def web_start():calls.append('http')
        async def worker_claim(timeout):
            self.assertEqual(calls,['http']);calls.append('lock')
        async def telegram_start(**kwargs):
            self.assertEqual(calls,['http','lock']);calls.append('telegram')
        async def running():
            self.assertEqual(client.runtime_phase,'running');calls.append('running')
        async def disconnect():calls.append('disconnect')
        async def web_stop():calls.append('stop_http')
        with patch.object(asyncio.get_running_loop(),'add_signal_handler',side_effect=NotImplementedError()),patch.object(main,'start_webapp',new=AsyncMock(side_effect=web_start)),patch.object(main,'wait_for_worker',new=AsyncMock(side_effect=worker_claim)),patch.object(main,'initialize_user_session',new=AsyncMock()),patch.object(main,'schedule_physics_info',new=AsyncMock()),patch.object(main,'schedule_learning_maintenance',new=AsyncMock()),patch.object(webapp,'stop_webapp',new=AsyncMock(side_effect=web_stop)),patch.object(client,'start',new=AsyncMock(side_effect=telegram_start),create=True),patch.object(client,'run_until_disconnected',new=AsyncMock(side_effect=running),create=True),patch.object(client,'disconnect',new=AsyncMock(side_effect=disconnect),create=True),patch.object(db,'create_files'),patch.object(db,'insert_default_data'),patch.object(db,'close',side_effect=lambda:calls.append('release_lock')):
            await main.main()
        self.assertEqual(calls,['http','lock','telegram','running','stop_http','disconnect','release_lock'])
    async def test_cancel_during_wait_closes_http_without_starting_telegram(self):
        waiting=asyncio.Event()
        async def blocked(timeout):
            waiting.set();await asyncio.Event().wait()
        with patch.object(asyncio.get_running_loop(),'add_signal_handler',side_effect=NotImplementedError()),patch.object(main,'start_webapp',new=AsyncMock()),patch.object(main,'wait_for_worker',new=AsyncMock(side_effect=blocked)),patch.object(webapp,'stop_webapp',new=AsyncMock()) as stop,patch.object(client,'start',new=AsyncMock(),create=True) as start,patch.object(client,'is_connected',return_value=False),patch.object(db,'close') as close:
            task=asyncio.create_task(main.main());await waiting.wait()
            start.assert_not_awaited();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        stop.assert_awaited_once();close.assert_called_once()
    async def test_telegram_start_failure_still_releases_lock(self):
        with patch.object(asyncio.get_running_loop(),'add_signal_handler',side_effect=NotImplementedError()),patch.object(main,'start_webapp',new=AsyncMock()),patch.object(main,'wait_for_worker',new=AsyncMock()),patch.object(webapp,'stop_webapp',new=AsyncMock()) as stop,patch.object(client,'start',new=AsyncMock(side_effect=RuntimeError('test start failure')),create=True),patch.object(client,'is_connected',return_value=False),patch.object(db,'close') as close,self.assertLogs(level='ERROR'):
            with self.assertRaisesRegex(RuntimeError,'test start failure'):await main.main()
        stop.assert_awaited_once();close.assert_called_once()
