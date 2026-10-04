"""Opt-in destructive tests for a disposable database named bot4stage_test_*.
Never point TEST_POSTGRES_DSN at a production database.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from test_regressions import Database
import database

DSN=os.getenv('TEST_POSTGRES_DSN','')

@unittest.skipUnless(DSN,'TEST_POSTGRES_DSN is not configured; real PostgreSQL checks were not run')
class PostgreSQLIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        if not conninfo_to_dict(DSN).get('dbname','').startswith('bot4stage_test_'):
            raise ValueError('Integration tests require a disposable database named bot4stage_test_*')
        with psycopg.connect(DSN,autocommit=True) as connection:
            connection.execute('DROP SCHEMA public CASCADE')
            connection.execute('CREATE SCHEMA public')
        cls.folder=tempfile.TemporaryDirectory()
    @classmethod
    def tearDownClass(cls):cls.folder.cleanup()
    def instance(self):
        with patch.object(database,'DATABASE_URL',DSN),patch.object(database,'BACKUP_PATH',self.folder.name):return Database()
    def test_pool_concurrency_and_single_worker_lease(self):
        one=self.instance();two=self.instance()
        try:
            self.assertTrue(one.claim_worker());self.assertFalse(two.claim_worker())
            one.add_user(700000001,None,'PostgreSQL','Test');one.set_user_stage(700000001,'PostgreSQL Test',1)
            with ThreadPoolExecutor(max_workers=6) as pool:
                ids=list(pool.map(lambda _:one.add_stage_content(1,'mathematics',1,'text',None,text='concurrent'),range(12)))
            self.assertEqual(len(set(ids)),12)
            self.assertTrue(one.ping());self.assertTrue(one.worker_lease_alive())
        finally:one.close();two.close()
    def test_dump_restore_preserves_data(self):
        one=self.instance()
        try:
            one.add_user(700000002,None,'Restored','Student')
            one.set_user_stage(700000002,'Restored Student',1)
            backup=one.create_backup()
            one.ban_user(700000002);self.assertTrue(one.is_banned(700000002))
            one.restore_postgres_backup(backup)
        finally:one.close()
        reopened=self.instance()
        try:self.assertFalse(reopened.is_banned(700000002));self.assertEqual(reopened.get_user_stage(700000002)[0],1)
        finally:reopened.close()
