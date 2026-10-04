import asyncio
import importlib
import sqlite3
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from test_regressions import db,Database,main,utils,webapp,signed,client

class FeatureDatabaseTests(unittest.TestCase):
    def test_fourth_stage_only_nuclear_lab(self):
        self.assertEqual([x[1] for x in db.get_stage_subjects_by_category(4,'lab')],['nuclear_physics_lab'])
    def test_course_experiments_separate(self):
        for stage in (1,2,3,4):
            count=4 if stage==4 else 6
            key=db.get_stage_subjects_by_category(stage,'lab')[0][1]
            self.assertEqual(len(db.get_chapters(stage,key)),count*2)
            first=db.lab_slot(stage,1,1);last=db.lab_slot(stage,2,count)
            one=db.add_stage_content(stage,key,first,'text',None,text='first course')
            two=db.add_stage_content(stage,key,last,'text',None,text='second course')
            self.assertIn(one,[x[0] for x in db.get_stage_content(stage,key,first)])
            self.assertNotIn(two,[x[0] for x in db.get_stage_content(stage,key,first)])
            with self.assertRaises(ValueError):db.lab_slot(stage,1,count+1)
    def test_laser_ten_chapters(self):
        self.assertEqual([x['id'] for x in db.get_chapters(4,'laser')],list(range(1,11)))
        self.assertEqual(len(db.get_chapters(4,'laser_exp')),10)
        c=db.add_stage_content(4,'laser',10,'text',None,text='laser chapter ten')
        self.assertEqual(db.get_stage_content_by_id(c)[3],10)
    def test_developer_chapter_management(self):
        c=db.add_chapter(1,'mathematics','فصل مخصص',123)
        content=db.add_stage_content(1,'mathematics',c,'text',None,text='retained content')
        self.assertTrue(db.remove_chapter(1,'mathematics',c,123))
        self.assertTrue(db.get_stage_content_by_id(content));self.assertFalse(db.is_valid_chapter(1,'mathematics',c))
        c2=db.add_chapter(1,'mathematics','فصل جديد',123);self.assertGreater(c2,c)
        with self.assertRaises(PermissionError):db.add_chapter(1,'mathematics','ممنوع',999999)
        other=Database();self.assertTrue(other.is_valid_chapter(1,'mathematics',c2));other.close()
    def test_search_all_and_offset(self):
        query='uniquepagination'+str(time.time_ns())
        for _ in range(111):db.add_stage_content(1,'mathematics',1,'text',None,text=query)
        self.assertEqual(len(db.search_content(query,1,limit=None)),111)
        first=db.search_content(query,1,limit=5,offset=0);second=db.search_content(query,1,limit=5,offset=5)
        self.assertEqual(len(first),5);self.assertFalse({x[0]['id'] for x in first}&{x[0]['id'] for x in second})
    def test_legacy_chapter_constraint_migration(self):
        import database
        with tempfile.TemporaryDirectory() as folder,patch.object(database,'DATABASE_PATH',folder+'/legacy.db'):
            original=Database();original.add_user(900001,None,'Legacy','User');original.set_user_stage(900001,'Legacy User',1)
            content=original.add_stage_content(1,'mathematics',1,'text',None,text='preserved');original.toggle_favorite(900001,content);original.close()
            con=sqlite3.connect(folder+'/legacy.db');con.execute('PRAGMA foreign_keys=OFF')
            schema=con.execute("SELECT sql FROM sqlite_master WHERE name='content'").fetchone()[0]
            schema=schema.replace('CREATE TABLE content','CREATE TABLE content_legacy',1).replace('CHECK(chapter > 0)','CHECK(chapter BETWEEN 1 AND 6)')
            con.execute(schema);con.execute('INSERT INTO content_legacy SELECT * FROM content');con.execute('DROP TABLE content');con.execute('ALTER TABLE content_legacy RENAME TO content');con.commit();con.close()
            upgraded=Database();self.assertTrue(upgraded.is_favorite(900001,content));self.assertEqual(upgraded.get_stage_content_by_id(content)[7],'preserved')
            upgraded.add_stage_content(4,'laser',10,'text',None,text='allowed after migration')
            self.assertFalse(upgraded._execute('PRAGMA foreign_key_check').fetchall());upgraded.close()

class FeatureAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid=8000000+int(time.time_ns()%1000000);db.add_user(self.uid,None,'Test','User');db.set_user_stage(self.uid,'Test User',4);client.send_message.reset_mock()
    def event(self,uid=None,data=b''):
        return types.SimpleNamespace(sender_id=uid or self.uid,chat_id=uid or self.uid,is_private=True,data=data,answer=AsyncMock(),reply=AsyncMock(),edit=AsyncMock(),raw_text='hello',text='hello',message_id=1)
    async def test_four_experiment_buttons(self):
        e=self.event();await main.handle_lab_course(e,'select',['4','1','nuclear_physics_lab'])
        flat=[b for row in e.edit.await_args.kwargs['buttons'] for b in row]
        self.assertEqual(sum(b.type.data.startswith(b'lab_experiment:') for b in flat),4)
    async def test_six_experiment_buttons_other_stages(self):
        db.add_admin(self.uid,None,'Admin',123)
        e=self.event();key=db.get_stage_subjects_by_category(2,'lab')[0][1];await main.handle_lab_course(e,'select',['2','2',key])
        flat=[b for row in e.edit.await_args.kwargs['buttons'] for b in row]
        self.assertEqual(sum(b.type.data.startswith(b'lab_experiment:') for b in flat),6)
    async def test_experiment_upload_and_send(self):
        e=self.event();db.add_admin(self.uid,None,'Admin',123)
        await main.handle_lab_experiment(e,['lab_experiment','4','nuclear_physics_lab','2','4'])
        self.assertTrue(any(b.type.data.startswith(b'stage_content:add:4:nuclear_physics_lab:8') for row in e.edit.await_args.kwargs['buttons'] for b in row))
        db.add_stage_content(4,'nuclear_physics_lab',8,'text',None,text='experiment8')
        with patch.object(utils.ContentSender,'send_stage_content_list',new=AsyncMock()) as sendlist,patch.object(utils.ContentSender,'send_stage_single_content',new=AsyncMock()) as sendone:
            await main.handle_lab_experiment(e,['lab_send','4','nuclear_physics_lab','2','4'],True)
            self.assertTrue(sendlist.await_count or sendone.await_count)
    async def test_laser_keyboard(self):
        from keyboards import Keyboards
        buttons=Keyboards.stage_chapters_menu(4,'laser',True,True)
        callbacks=[b.type.data for row in buttons for b in row]
        self.assertIn(b'stage_chapter:4:laser:10',callbacks);self.assertTrue(any(x.startswith(b'chapter_admin:') for x in callbacks))
    async def test_quiz_admin_wizard_and_duplicate_click(self):
        db.add_admin(self.uid,None,'Admin',123);e=self.event()
        await main.handle_quiz_admin(e,['subject','4','laser'])
        await main.handle_quiz_admin(e,['type','4','laser','multiple_choice'])
        e.raw_text='ما هو الليزر؟';await main.handle_upload_messages(e)
        e.raw_text='ضوء\nصوت\nحرارة\nماء';await main.handle_upload_messages(e)
        before=len(db._read_data('quizzes'))
        await main.handle_quiz_admin(e,['correct','0']);await main.handle_quiz_admin(e,['correct','0'])
        quizzes=db._read_data('quizzes');self.assertEqual(len(quizzes),before+1);self.assertEqual(quizzes[-1]['subject_key'],'laser')
    async def test_quiz_admin_revocation(self):
        db.add_admin(self.uid,None,'Admin',123);e=self.event();await main.handle_quiz_admin(e,['subject','4','laser']);db.remove_admin(self.uid)
        before=len(db._read_data('quizzes'));await main.process_quiz_input(e,'quiz_question',{'stage':4,'key':'laser'});self.assertEqual(len(db._read_data('quizzes')),before)
    async def test_search_pages_five_and_edit(self):
        e=self.event();session={'query':'test','stage':4,'at':time.time(),'results':[{'content_id':i+1,'channel_title':'laser','message_text':'test'} for i in range(12)]};main.search_sessions[self.uid]=session
        await main.display_results_page(e,session,0)
        callbacks=[b.type.data for row in e.reply.await_args.kwargs['buttons'] for b in row]
        self.assertEqual(sum(x.startswith(b'stage_content:view:') for x in callbacks),5);self.assertIn(b'search_page:1',callbacks)
        await main.handle_search_page(e,1,[]);callbacks=[b.type.data for row in e.edit.await_args.kwargs['buttons'] for b in row]
        self.assertEqual(sum(x.startswith(b'stage_content:view:') for x in callbacks),5);self.assertIn(b'search_page:2',callbacks)
    async def test_remote_search_no_five_result_cap(self):
        calls=[]
        class Search:
            async def get_entity(self,key):return types.SimpleNamespace(username='testchannel',id=123)
            def iter_messages(self,entity,**kwargs):
                calls.append(kwargs)
                async def messages():
                    for i in range(17):yield types.SimpleNamespace(id=i+1,text='كلمة البحث',date=None)
                return messages()
        rows=await utils.ChannelSearch.search_in_single_channel_user_session(Search(),'كلمة',(1,'testchannel','Channel',4),4,None)
        self.assertEqual(len(rows),17);self.assertEqual(calls[0]['limit'],30)
    async def test_web_dynamic_chapters_and_experiments(self):
        from aiohttp.test_utils import TestClient,TestServer
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_get('/api/subjects/{k}/chapters',webapp.chapters);app.router.add_get('/api/content',webapp.content_list)
        async with TestClient(TestServer(app)) as http:
            headers={'X-Telegram-Init-Data':signed(self.uid)}
            laser=await http.get('/api/subjects/laser/chapters',headers=headers);self.assertEqual(len((await laser.json())['chapters']),10)
            labs=await http.get('/api/subjects/nuclear_physics_lab/chapters',headers=headers);self.assertEqual(len((await labs.json())['items']),8)
            content=await http.get('/api/content?subject=laser&chapter=10',headers=headers);self.assertEqual(content.status,200)
    async def test_web_search_five_results(self):
        from aiohttp.test_utils import TestClient,TestServer
        query='webpage'+str(time.time_ns())
        for _ in range(12):db.add_stage_content(4,'laser',1,'text',None,text=query)
        app=webapp.web.Application(middlewares=[webapp.api_errors]);app.router.add_get('/api/search',webapp.search)
        async with TestClient(TestServer(app)) as http:
            headers={'X-Telegram-Init-Data':signed(self.uid)}
            response=await http.get('/api/search?q='+query,headers=headers);data=await response.json();self.assertEqual(len(data['items']),5);self.assertTrue(data['has_more'])
            response=await http.get('/api/search?q='+query+'&page=2',headers=headers);data=await response.json();self.assertEqual(len(data['items']),2);self.assertFalse(data['has_more'])
