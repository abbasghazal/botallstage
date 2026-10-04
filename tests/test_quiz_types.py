"""Quiz management, typed answers and authenticated API regressions."""
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
from test_regressions import Database, db, main, utils, webapp, signed
from quiz_tools import normalize_answer, public_quiz


def offer(uid, qid):
    settings = db._read_data('settings')
    settings.setdefault('quiz_offers', {})[str(uid)] = {'quiz_id': qid, 'at': time.time()}
    db._write_data('settings', settings)


class QuizDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.uid = 400000000 + time.time_ns() % 100000000
        db.add_user(self.uid, None, 'Quiz', 'Student')
        db.set_user_stage(self.uid, 'Quiz Student', 4)

    def test_legacy_and_true_false(self):
        qid = db.add_quiz(4, 'laser', 'Legacy', ['A', 'B'], 1, 123)
        db._execute('DELETE FROM collection_records WHERE collection=? AND record_key=?', ('quizzes', json.dumps([qid], separators=(',', ':'))))
        self.assertEqual(db.get_quiz(qid)['question_type'], 'multiple_choice')
        qid = db.add_quiz(4, 'laser', 'Light is a wave', [], 0, 123, 'true_false')
        self.assertEqual(db.get_quiz(qid)['options'], ['صح', 'خطأ'])
        offer(self.uid, qid)
        self.assertTrue(db.record_quiz_attempt(self.uid, qid, 0))
        self.assertTrue(db.record_quiz_attempt(self.uid, qid, 1))
        self.assertEqual(len([x for x in db._read_data('quiz_attempts') if x['quiz_id'] == qid]), 1)

    def test_blank_normalization_alternatives_and_persistence(self):
        qid = db.add_quiz(4, 'laser', 'العدد ___', [], 0, 123, 'fill_blank', ['أَرْبَعَة', '٤'])
        other = Database()
        try: self.assertEqual(other.get_quiz(qid)['accepted_answers'], ['أَرْبَعَة', '٤'])
        finally: other.close()
        offer(self.uid, qid)
        self.assertTrue(db.record_quiz_attempt(self.uid, qid, '  اربعة  '))
        row = next(x for x in db._read_data('quiz_attempts') if x['quiz_id'] == qid)
        self.assertEqual(row['answer_text'], 'اربعة')
        self.assertEqual(normalize_answer('٤ ۵'), '4 5')
        payload = public_quiz(db.get_quiz(qid))
        self.assertNotIn('accepted_answers', payload)
        self.assertNotIn('correct_index', payload)
        self.assertEqual(payload['options'], [])

    def test_blank_wrong_and_invalid_types(self):
        qid = db.add_quiz(4, 'laser', '___', [], 0, 123, 'fill_blank', ['four'])
        offer(self.uid, qid)
        for bad in (0, True, '', ' '*5, 'a'*101):
            self.assertIsNone(db.record_quiz_attempt(self.uid, qid, bad))
        self.assertFalse(db.record_quiz_attempt(self.uid, qid, 'five'))
        for bad in ([], [''], ['ـ'], ['a'*101], ['a']*11):
            with self.assertRaises(ValueError): db.add_quiz(4, 'laser', '___', [], 0, 123, 'fill_blank', bad)
        with self.assertRaises(ValueError): db.add_quiz(4, 'laser', 'Q', [], 2, 123, 'true_false')

    def test_delete_permission_scope_and_historical_results(self):
        qid = db.add_quiz(4, 'laser', 'Delete', ['A', 'B'], 0, 123)
        offer(self.uid, qid)
        self.assertTrue(db.record_quiz_attempt(self.uid, qid, 0))
        before = db.get_user_progress(self.uid)
        with self.assertRaises(PermissionError): db.delete_quiz(qid, self.uid)
        db.add_admin(self.uid, None, 'Admin', 123)
        other_qid = db.add_quiz(1, 'mathematics', 'Wrong scope', ['A', 'B'], 0, 123)
        with self.assertRaises(PermissionError): db.delete_quiz(other_qid, self.uid)
        with self.assertRaises(PermissionError): db.get_manageable_quizzes(self.uid, 1)
        self.assertTrue(db.delete_quiz(qid, self.uid))
        self.assertFalse(db.delete_quiz(qid, self.uid))
        self.assertFalse(db.get_quiz(qid)['enabled'])
        self.assertEqual(db.get_quiz(qid)['deleted_by'], self.uid)
        self.assertIsNone(db.record_quiz_attempt(self.uid, qid, 0))
        self.assertEqual(db.get_user_progress(self.uid), before)
        self.assertNotIn(qid, [x['id'] for x in db.get_manageable_quizzes(self.uid, 4, 0, 21)])
        self.assertTrue(db.delete_quiz(other_qid, 123))

    def test_deleted_question_never_offered(self):
        import database
        import tempfile
        with tempfile.TemporaryDirectory() as folder, patch.object(database, 'DATABASE_PATH', folder+'/isolated.db'):
            isolated = Database()
            try:
                qid = isolated.add_quiz(4, 'laser', 'Q', ['A', 'B'], 0, 123)
                isolated.delete_quiz(qid, 123)
                self.assertIsNone(isolated.get_random_quiz(4))
            finally: isolated.close()


class QuizFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid = 600000000 + time.time_ns() % 100000000
        db.add_user(self.uid, None, 'Quiz', 'Student')
        db.set_user_stage(self.uid, 'Quiz Student', 4)
    def event(self, text=''):
        return types.SimpleNamespace(sender_id=self.uid, chat_id=self.uid, is_private=True, raw_text=text, text=text, data=b'', answer=AsyncMock(), edit=AsyncMock(), reply=AsyncMock())

    async def test_true_false_wizard(self):
        db.add_admin(self.uid, None, 'Admin', 123)
        e = self.event()
        await main.handle_quiz_admin(e, ['type', '4', 'laser', 'tf'])
        e.raw_text = 'الضوء موجة'; await main.handle_upload_messages(e)
        self.assertEqual(main.pending_user_actions.get(self.uid)['data']['options'], ['صح', 'خطأ'])
        await main.handle_quiz_admin(e, ['correct', '0'])
        q = db.get_manageable_quizzes(self.uid, 4)[0]
        self.assertEqual(q['question_type'], 'true_false')

    async def test_blank_wizard_and_student_reply(self):
        db.add_admin(self.uid, None, 'Admin', 123)
        e = self.event()
        await main.handle_quiz_admin(e, ['type', '4', 'laser', 'blank'])
        e.raw_text = 'عدد ___'; await main.handle_upload_messages(e)
        e.raw_text = '٤\nأربعة'; await main.handle_upload_messages(e)
        q = db.get_manageable_quizzes(self.uid, 4)[0]
        self.assertEqual(q['question_type'], 'fill_blank')
        offer(self.uid, q['id'])
        with patch.object(main.db, 'get_random_quiz', return_value=q):
            await main.handle_learning(e, 'quiz', [])
        self.assertEqual(main.pending_user_actions.get(self.uid)['action'], 'quiz_answer_blank')
        e.raw_text = '4'; await main.handle_upload_messages(e)
        self.assertIn('إجابة صحيحة', e.reply.await_args.args[0])
        self.assertIsNone(main.pending_user_actions.get(self.uid))

    async def test_delete_confirmation_and_revocation(self):
        db.add_admin(self.uid, None, 'Admin', 123)
        qid = db.add_quiz(4, 'laser', 'Delete me', ['A','B'], 0, 123)
        e = self.event()
        await main.handle_quiz_admin(e, ['delete', str(qid), '0'])
        self.assertTrue(db.get_quiz(qid)['enabled'])
        self.assertEqual(e.edit.await_args.kwargs['buttons'][0][0].type.data, f'quiz_admin:confirm_delete:{qid}:0'.encode())
        db.remove_admin(self.uid)
        await main.handle_quiz_admin(e, ['confirm_delete', str(qid), '0'])
        self.assertTrue(db.get_quiz(qid)['enabled'])
        db.add_admin(self.uid, None, 'Admin', 123)
        await main.handle_quiz_admin(e, ['confirm_delete', str(qid), '0'])
        self.assertFalse(db.get_quiz(qid)['enabled'])

    async def test_callback_size_all_subjects(self):
        db.add_admin(self.uid, None, 'Admin', 123)
        e = self.event()
        for stage in (1, 2, 3, 4):
            db.set_admin_stages(123, self.uid, [stage])
            for _, key in db.get_stage_subjects_by_category(stage, 'subjects'):
                await main.handle_quiz_admin(e, ['subject', str(stage), key])
                for row in e.edit.await_args.kwargs['buttons']:
                    for button in row: self.assertLessEqual(len(button.type.data), 64)

    async def test_api_typed_answers_and_deleted_question(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = webapp.web.Application(middlewares=[webapp.api_errors])
        app.router.add_get('/api/quiz', webapp.quiz)
        app.router.add_post('/api/quiz/{i}/answer', webapp.answer)
        headers = {'X-Telegram-Init-Data': signed(self.uid)}
        qid = db.add_quiz(4, 'laser', '___', [], 0, 123, 'fill_blank', ['٤'])
        q = db.get_quiz(qid); offer(self.uid, qid)
        with patch.object(utils, 'check_subscription', new=AsyncMock(return_value=True)), patch.object(webapp.db, 'get_random_quiz', return_value=q):
            async with TestClient(TestServer(app)) as http:
                response = await http.get('/api/quiz', headers=headers)
                payload = (await response.json())['quiz']
                self.assertEqual(payload['question_type'], 'fill_blank')
                self.assertNotIn('accepted_answers', payload)
                response = await http.post(f'/api/quiz/{qid}/answer', headers=headers, json={'answer':0})
                self.assertEqual(response.status, 400)
                response = await http.post(f'/api/quiz/{qid}/answer', headers=headers, json={'answer':'4'})
                self.assertEqual(response.status, 200); self.assertTrue((await response.json())['correct'])
                db.delete_quiz(qid, 123)
                response = await http.post(f'/api/quiz/{qid}/answer', headers=headers, json={'answer':'4'})
                self.assertEqual(response.status, 400)
                tf = db.add_quiz(4, 'laser', 'Q', [], 1, 123, 'true_false'); offer(self.uid, tf)
                response = await http.post(f'/api/quiz/{tf}/answer', headers=headers, json={'answer':True})
                self.assertEqual(response.status, 400)
                response = await http.post(f'/api/quiz/{tf}/answer', headers=headers, json={'answer':1})
                self.assertEqual(response.status, 200); self.assertTrue((await response.json())['correct'])
