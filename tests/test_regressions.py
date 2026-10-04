"""Offline integration tests. Never loads .env or connects to Telegram."""
import asyncio
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
TMP=tempfile.TemporaryDirectory()
class FakeClient:
    def __init__(self):
        self.send_message=AsyncMock();self.send_file=AsyncMock();self.background_tasks=[]
    def on(self,*args):return lambda f:f
    def is_connected(self):return True
client=FakeClient();cfg=types.ModuleType('config')
cfg.DEVELOPER_ID=123;cfg.DATA_KEYS=('users','users_stages','subjects','admins','required_channels','comments','comments_meta','search_channels','support_tickets','ai_usage','settings','stage_content','physics_channel','physics_requests','favorites','progress','quizzes','quiz_attempts','notification_preferences','scheduled_content','audit_log')
cfg.DATABASE_URL='';cfg.DATABASE_PATH=TMP.name+'/test.db';cfg.BACKUP_PATH=TMP.name+'/backups';cfg.BOT_TOKEN='test:token';cfg.MINI_APP_PORT=0;cfg.MINI_APP_URL='';cfg.STORAGE_CHANNEL_ID=0;cfg.OPENAI_API_KEY='';cfg.client=client;cfg.user_client=None;cfg.loop=asyncio.new_event_loop();sys.modules['config']=cfg
from database import Database,db,IRAQ_TZ
import main,webapp,utils
from datetime import datetime,timedelta

def signed(uid):
    data={'auth_date':str(int(time.time())),'user':json.dumps({'id':uid,'first_name':'Test'})}
    key=hmac.new(b'WebAppData',cfg.BOT_TOKEN.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(key,'\n'.join(f'{k}={data[k]}' for k in sorted(data)).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)

class DatabaseTests(unittest.TestCase):
    n=1000
    def setUp(self):
        db._execute('DELETE FROM ai_reservations');db._connection.commit()
        type(self).n+=1;self.uid=type(self).n
        db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',1)
    def content(self):return db.add_stage_content(1,'mathematics',1,'text',None,text='unique test text')
    def test_user_projection(self):
        self.assertEqual(db._execute('SELECT stage FROM student_profiles WHERE telegram_id=?',(self.uid,)).fetchone()[0],1)
    def test_delete_cascade_and_no_reuse(self):
        c=self.content();db.toggle_favorite(self.uid,c);db.mark_content_viewed(self.uid,c);db.add_comment(self.uid,c,'hello',1);db.schedule_content(c,(datetime.now(IRAQ_TZ)+timedelta(days=1)).isoformat(),123)
        db.delete_stage_content(c);c2=self.content()
        self.assertGreater(c2,c);self.assertFalse(db.is_favorite(self.uid,c2));self.assertEqual(db.get_user_progress(self.uid)['viewed'],0);self.assertFalse(db.get_comments_for_content(c));self.assertFalse(any(x['content_id']==c for x in db._read_data('scheduled_content')))
    def test_transaction_rollback(self):
        before=len(db._read_data('stage_content'))
        with self.assertRaises(Exception):db.add_stage_content(1,'missing_subject',1,'text',None,text='bad')
        self.assertEqual(len(db._read_data('stage_content')),before)
        self.content() # failed transaction did not poison the connection
    def test_quiz_replay_and_offer(self):
        q=db.add_quiz(1,'mathematics','Q',['A','B'],0,123)
        self.assertIsNone(db.record_quiz_attempt(self.uid,q,0))
        for _ in range(30):
            if db.get_random_quiz(1,self.uid)['id']==q:break
        settings=db._read_data('settings');settings.setdefault('quiz_offers',{})[str(self.uid)]={'quiz_id':q,'at':time.time()};db._write_data('settings',settings)
        self.assertTrue(db.record_quiz_attempt(self.uid,q,0));self.assertTrue(db.record_quiz_attempt(self.uid,q,0))
        self.assertEqual(sum(x['user_id']==self.uid and x['quiz_id']==q for x in db._read_data('quiz_attempts')),1)
    def test_wrong_stage_quiz(self):
        q=db.add_quiz(2,'mathematics','Q',['A','B'],0,123) if db.get_stage_subject(2,'mathematics') else db.add_quiz(2,db.get_stage_subjects_by_category(2,'subjects')[0][1],'Q',['A','B'],0,123)
        self.assertIsNone(db.record_quiz_attempt(self.uid,q,0))
    def test_registration_immutable(self):
        with self.assertRaises(ValueError):db.set_user_stage(self.uid,'Other User',2)
    def test_comment_wrong_stage(self):
        key=db.get_stage_subjects_by_category(2,'subjects')[0][1];c=db.add_stage_content(2,key,1,'text',None,text='other')
        self.assertIsNone(db.add_comment(self.uid,c,'hello',2))
    def test_rate_limit_persistent(self):
        self.assertTrue(db.rate_limit(self.uid,'test',1,60));self.assertFalse(db.rate_limit(self.uid,'test',1,60))
        other=Database();self.assertFalse(other.rate_limit(self.uid,'test',1,60));other.close()
    def test_budget_reservation(self):
        token=db.reserve_ai(self.uid,80,100);self.assertTrue(token);self.assertIsNone(db.reserve_ai(self.uid,30,100));db.settle_ai(self.uid,token,50);self.assertTrue(db.reserve_ai(self.uid,40,100))
    def test_sqlite_backup(self):
        path=db.create_backup();self.assertTrue(Path(path).stat().st_size>0)
    def test_corrupt_data_fails_closed(self):
        db._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,?,?)',('physics_requests','corrupt',0,'broken'));db._connection.commit()
        try:
            with self.assertRaises(ValueError):db._read_data('physics_requests')
        finally:
            db._execute('DELETE FROM collection_records WHERE collection=? AND record_key=?',('physics_requests','corrupt'));db._connection.commit()
    def test_concurrent_ids(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(lambda _:self.content(),range(12)))
        self.assertEqual(len(set(ids)),12)

    def test_upload_atomic_once(self):
        db.add_admin(self.uid,None,'Admin',123)
        db.set_workflow(self.uid,'upload',{'step':'content','stage':1,'subject_key':'mathematics','chapter_num':1,'content_type':'text'})
        payload=dict(stage=1,subject_key='mathematics',chapter_num=1,content_type='text',file_id=None,text='uploaded',added_by=self.uid)
        cid=db.finish_upload(self.uid,**payload);self.assertTrue(db.get_stage_content_by_id(cid))
        with self.assertRaises(PermissionError):db.finish_upload(self.uid,**payload)

    def test_workflow_restart_and_expiry(self):
        db.set_workflow(self.uid,'pending',{'action':'register_stage','data':{'full_name':'Test User'}})
        other=Database();self.assertEqual(other.get_workflow(self.uid,'pending')['action'],'register_stage');other.close()
        with patch('database.time.time',return_value=time.time()+901):self.assertIsNone(db.get_workflow(self.uid,'pending'))
    def test_budget_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:tokens=list(pool.map(lambda _:db.reserve_ai(self.uid,25,100),range(20)))
        self.assertEqual(sum(bool(x) for x in tokens),4)
    def test_schedule_future_only(self):
        c=self.content()
        with self.assertRaises(ValueError):db.schedule_content(c,(datetime.now(IRAQ_TZ)-timedelta(days=1)).isoformat(),123)
    def test_relational_changes_survive_restart(self):
        c=self.content();db._execute('UPDATE content SET body=? WHERE id=?',('stale',c));db._connection.commit()
        other=Database();self.assertEqual(other.get_stage_content_by_id(c)[7],'stale');other.close()
    def test_worker_lease(self):
        import database
        with tempfile.TemporaryDirectory() as folder,patch.object(database,'DATABASE_PATH',folder+'/worker.db'):
            one=Database();two=Database()
            self.assertTrue(one.claim_worker());self.assertFalse(two.claim_worker());one.close();self.assertTrue(two.claim_worker());two.close()

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=5000+int(time.time_ns()%1000000);db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',1)
        client.send_message.reset_mock()
    def event(self,uid=None,data=b''):
        return types.SimpleNamespace(sender_id=uid or self.uid,chat_id=uid or self.uid,is_private=True,data=data,answer=AsyncMock(),reply=AsyncMock(),edit=AsyncMock(),raw_text='hello',message_id=1)
    async def test_long_name_buttons(self):
        from keyboards import Keyboards
        for row in Keyboards.stage_selection_menu('عباس '*100):
            for b in row:self.assertLessEqual(len(b.type.data),64)
    async def test_upload_revocation(self):
        db.add_admin(self.uid,None,'Admin',123);h=utils.ContentUploadHandler();await h.start_upload_session(self.uid,1,'mathematics',1,'text');db.remove_admin(self.uid)
        result=await h.process_description(self.uid,'description');self.assertIsInstance(result,tuple);self.assertFalse(h.get_user_session(self.uid))
    async def test_upload_type(self):
        db.add_admin(self.uid,None,'Admin',123);h=utils.ContentUploadHandler();await h.start_upload_session(self.uid,1,'mathematics',1,'video');await h.process_description(self.uid,'description')
        result=await h.process_content(self.uid,types.SimpleNamespace(media=True,video=None));self.assertIn('نوع',result)
    async def test_student_stats_denied(self):
        e=self.event(data=b'admin:users_page:all:0');await main.handle_users_page_callback(e);e.edit.assert_not_awaited()
    async def test_content_stage_denied(self):
        key=db.get_stage_subjects_by_category(2,'subjects')[0][1];c=db.add_stage_content(2,key,1,'text',None,text='other');e=self.event();await main.handle_view_content(e,c,False);client.send_message.assert_not_awaited()
    async def test_empty_users_page(self):
        e=self.event();await main.display_users_page(e,[],0,'all');e.edit.assert_awaited_once()
    async def test_lab_content_visible(self):
        key=db.get_stage_subjects_by_category(1,'lab')[0][1];db.add_stage_content(1,key,1,'text',None,text='lab');e=self.event()
        with patch.object(utils.ContentSender,'send_stage_content_list',new=AsyncMock()) as send:
            await main.handle_stage_subject(e,1,key,False)
            e.edit.assert_awaited_once()
            self.assertTrue(any(b.type.data.startswith(b'lab_course:') for row in e.edit.await_args.kwargs['buttons'] for b in row))
    async def test_content_pagination(self):
        values=[(i,'text','D','date',i) for i in range(45)]
        await utils.ContentSender.send_stage_content_list(self.uid,1,'mathematics',1,values,page=1)
        buttons=client.send_message.await_args.kwargs['buttons'];flat=[b for row in buttons for b in row]
        self.assertTrue(any(b.type.data.startswith(b'content_page:') for b in flat));self.assertEqual(sum(b.type.data.startswith(b'stage_content:view') for b in flat),20)
    async def test_web_auth_and_subscription(self):
        r=types.SimpleNamespace(headers={'X-Telegram-Init-Data':signed(self.uid)})
        with patch.object(utils,'check_subscription',new=AsyncMock(return_value=False)):
            with self.assertRaises(webapp.web.HTTPForbidden):await webapp.ident(r)
        with patch.object(utils,'check_subscription',new=AsyncMock(return_value=True)):
            self.assertEqual((await webapp.ident(r))[0],self.uid)
        r.headers['X-Telegram-Init-Data']='invalid'
        with self.assertRaises(webapp.web.HTTPUnauthorized):await webapp.ident(r)
    async def test_api_json_errors(self):
        from aiohttp.test_utils import TestClient,TestServer
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_get('/api/me',webapp.me)
        async with TestClient(TestServer(app)) as http:
            r=await http.get('/api/me');self.assertEqual(r.status,401);self.assertIn('error',await r.json())
    async def test_send_rate_limit(self):
        for _ in range(10):await webapp._limit(self.uid,'send')
        with self.assertRaises(webapp.web.HTTPTooManyRequests):await webapp._limit(self.uid,'send')


    async def test_health_background_failure(self):
        task=asyncio.create_task(asyncio.sleep(0));await task;client.background_tasks=[task]
        response=await webapp.health(None);self.assertEqual(response.status,503);client.background_tasks=[]
    async def test_registration_boolean_rejected(self):
        from aiohttp.test_utils import TestClient,TestServer
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_post('/api/register',webapp.register)
        async with TestClient(TestServer(app)) as http:
            response=await http.post('/api/register',headers={'X-Telegram-Init-Data':signed(self.uid+9999999)},json={'full_name':'Test User','stage':True})
            self.assertEqual(response.status,400)

if __name__=='__main__':unittest.main()
