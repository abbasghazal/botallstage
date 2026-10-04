import json
import os
import threading
import time
import logging
import sqlite3
import random
import shutil
import subprocess
import functools
import contextlib
import hashlib
import uuid
from json import JSONDecodeError
from datetime import datetime, date, timezone, timedelta
from config import DEVELOPER_ID, DATA_KEYS, DATABASE_PATH, DATABASE_URL, BACKUP_PATH
IRAQ_TZ = timezone(timedelta(hours=3), name='Asia/Baghdad')

class Database:

    def __init__(self,wait_for_worker=False,startup_timeout=300,stop_event=None):
        self._local = threading.local()
        self._pool = None
        self._lease_connection = None
        self._data_ready = False
        self.data_keys = DATA_KEYS
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._activity_times = {}
        self._worker_claimed = False
        self._worker_file = None
        self.database_path = DATABASE_PATH
        self.database_url = DATABASE_URL
        self.backup_path = BACKUP_PATH
        self._backend = 'postgresql' if self.database_url else 'sqlite'
        self._connection = self._connect_database()
        self._execute('CREATE TABLE IF NOT EXISTS app_data (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)')
        self._connection.commit()
        if wait_for_worker:
            deadline=time.monotonic()+startup_timeout
            try:
                while not self.claim_worker():
                    if time.monotonic()>=deadline or (stop_event and stop_event.is_set()):
                        raise RuntimeError('Timed out or stopped before obtaining worker lease')
                    time.sleep(0.25)
            except BaseException:
                self.close();raise
        try:
            self._create_relational_schema()
            self._data_ready = self._migration_done("authoritative_rows_v2")
            self.create_files()
            self.insert_default_data()
            self._upgrade_chapter_schema()
            self._last_comment_ts = {}
            logging.basicConfig(level=logging.INFO)
            if not self._data_ready:
                self._repair_projections()
                self._migrate_rows()
            self._projections_ready = True
            self._execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_unique_attempt ON quiz_attempts(telegram_id,quiz_id)')
            self._connection.commit()
            if self._backend == "postgresql":
                from psycopg_pool import ConnectionPool
                def configure(connection):
                    connection.execute("SELECT set_config('statement_timeout','30000',false)")
                    connection.commit()
                self._pool = ConnectionPool(self.database_url, min_size=1, max_size=8, timeout=15, kwargs={"connect_timeout": 15},configure=configure)
                self._pool.wait()
        except BaseException:
            self.close();raise

    @property
    def _connection(self):
        return getattr(self._local, 'connection', self._initial_connection)

    @_connection.setter
    def _connection(self, value):
        if not hasattr(self, '_initial_connection'):
            self._initial_connection = value
        else:
            self._local.connection = value

    @property
    def _transaction_depth(self):
        return getattr(self._local, 'depth', 0)

    @_transaction_depth.setter
    def _transaction_depth(self, value):
        self._local.depth = value

    # Field names preserve the legacy Python interface while SQL is authoritative.
    ROWS = {
        'users': ('users', ('user_id',), {'user_id':'telegram_id','username':'username','first_name':'first_name','last_name':'last_name','date_joined':'joined_at','last_active':'last_active','is_banned':'is_banned'}),
        'users_stages': ('student_profiles', ('user_id',), {'user_id':'telegram_id','full_name':'full_name','stage':'stage','date_registered':'registered_at'}),
        'subjects': ('subjects', ('stage','key'), {'stage':'stage','key':'subject_key','name_ar':'name_ar','name_en':'name_en','category':'category'}),
        'stage_content': ('content', ('id',), {'id':'id','stage':'stage','subject_key':'subject_key','chapter_num':'chapter','content_type':'content_type','file_id':'file_id','file_name':'file_name','text':'body','description':'description','content_number':'content_number','added_by':'added_by','storage_message_id':'storage_message_id'}),
        'support_tickets': ('support_tickets', ('id',), {'id':'id','user_id':'telegram_id','stage':'stage','message':'message','date':'created_at','status':'status'}),
        'comments': ('comments', ('id',), {'id':'id','user_id':'telegram_id','content_id':'content_id','stage':'stage','text':'body','date':'created_at'}),
        'progress': ('content_progress',('user_id','content_id'),{'user_id':'telegram_id','content_id':'content_id','views':'views','last_viewed':'last_viewed'}),
        'favorites': ('favorites', ('user_id','content_id'), {'user_id':'telegram_id','content_id':'content_id','created_at':'created_at'}),
        'quizzes': ('quizzes', ('id',), {'id':'id','stage':'stage','subject_key':'subject_key','question':'question','options':'options_json','correct_index':'correct_index','enabled':'enabled','added_by':'added_by','created_at':'created_at'}),
        'quiz_attempts': ('quiz_attempts', ('id',), {'id':'id','user_id':'telegram_id','quiz_id':'quiz_id','answer_index':'answer_index','correct':'is_correct','created_at':'attempted_at'})
    }

    def _record_key(self, key, row, index):
        if key in self.ROWS:
            fields = self.ROWS[key][1]
        elif 'id' in row:
            fields = ('id',)
        elif 'user_id' in row and 'content_id' in row:
            fields = ('user_id','content_id')
        elif 'user_id' in row:
            fields = ('user_id',)
        else:
            return str(index)
        return json.dumps([row.get(field) for field in fields], separators=(',',':'))

    def _migrate_rows(self):
        with self._lock:
            self._transaction_depth = 1
            try:
                if self._backend == 'postgresql':
                    self._execute('SELECT pg_advisory_xact_lock(42831004)')
                if self._migration_done('authoritative_rows_v2'):
                    self._data_ready = True
                    return
                for key in self.data_keys:
                    data = self._read_data(key)
                    items = data.items() if isinstance(data, dict) else enumerate(data)
                    for index, row in items:
                        ident = str(index) if isinstance(data,dict) else self._record_key(key,row,index)
                        payload = ({k:v for k,v in row.items() if k not in self.ROWS[key][2]} if key in self.ROWS else row)
                        self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,?,?) ON CONFLICT(collection,record_key) DO NOTHING', (key,ident,int(index) if isinstance(index,int) else 0,json.dumps(payload,ensure_ascii=False)))
                for row in self._read_data('stage_content'):
                    self._execute('INSERT INTO content_dates(id,added_at) VALUES (?,?) ON CONFLICT(id) DO NOTHING',(row['id'],row.get('date_added',datetime.now(IRAQ_TZ).isoformat())))
                for key in ('stage_content','comments','support_tickets','quizzes','quiz_attempts','scheduled_content'):
                    maximum = max((x.get('id',0) for x in self._read_data(key)),default=0)
                    self._execute('INSERT INTO id_counters(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (key,maximum))
                for user_id,record in self._read_data('ai_usage').items():
                    for day,tokens in record.get('daily_tokens',{}).items():
                        self._execute('INSERT INTO ai_daily(user_id,day,tokens) VALUES (?,?,?) ON CONFLICT(user_id,day) DO NOTHING',(int(user_id),day,int(tokens)))
                self._migration_mark('authoritative_rows_v2')
                self._connection.commit()
                self._data_ready = True
            except Exception:
                self._connection.rollback()
                raise
            finally:
                self._transaction_depth = 0

    def _create_relational_schema(self):
        """Create the normalized read model without deleting legacy data.

        ``app_data`` is retained solely as a migration source and compatibility
        layer for the Telegram bot.  Every statement is idempotent, so an
        interrupted deploy can safely be started again.
        """
        identity = 'BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY' if self._backend == 'postgresql' else 'INTEGER PRIMARY KEY'
        statements = ['CREATE TABLE IF NOT EXISTS users (telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, joined_at TEXT NOT NULL, last_active TEXT NOT NULL, is_banned INTEGER NOT NULL DEFAULT 0 CHECK(is_banned IN (0,1)))', 'CREATE TABLE IF NOT EXISTS student_profiles (telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE, full_name TEXT NOT NULL, stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), registered_at TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS subjects (stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), subject_key TEXT NOT NULL, name_ar TEXT NOT NULL, name_en TEXT, category TEXT NOT NULL, PRIMARY KEY(stage, subject_key))', f'CREATE TABLE IF NOT EXISTS content (id {identity}, stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), subject_key TEXT NOT NULL, chapter INTEGER NOT NULL CHECK(chapter > 0), content_type TEXT NOT NULL, file_id TEXT, file_name TEXT, body TEXT, description TEXT, content_number INTEGER NOT NULL, added_by BIGINT, storage_message_id BIGINT, UNIQUE(stage, subject_key, chapter, content_number), FOREIGN KEY(stage, subject_key) REFERENCES subjects(stage, subject_key))', f"CREATE TABLE IF NOT EXISTS support_tickets (id {identity}, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), message TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open')", f'CREATE TABLE IF NOT EXISTS comments (id {identity}, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), content_id BIGINT NOT NULL REFERENCES content(id) ON DELETE CASCADE, stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), body TEXT NOT NULL, created_at TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS favorites (telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE, content_id BIGINT NOT NULL REFERENCES content(id) ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY(telegram_id, content_id))', 'CREATE TABLE IF NOT EXISTS quizzes (id BIGINT PRIMARY KEY, stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4), subject_key TEXT, question TEXT NOT NULL, options_json TEXT NOT NULL, correct_index INTEGER NOT NULL CHECK(correct_index >= 0), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), added_by BIGINT, created_at TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS quiz_attempts (id BIGINT PRIMARY KEY, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), quiz_id BIGINT NOT NULL REFERENCES quizzes(id), answer_index INTEGER NOT NULL, is_correct INTEGER NOT NULL CHECK(is_correct IN (0,1)), attempted_at TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS workflow_state (telegram_id BIGINT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(telegram_id,kind))', 'CREATE INDEX IF NOT EXISTS idx_content_stage_subject_chapter ON content(stage, subject_key, chapter)', 'CREATE INDEX IF NOT EXISTS idx_content_search ON content(stage, description)', 'CREATE INDEX IF NOT EXISTS idx_comments_content_created ON comments(content_id, created_at)', 'CREATE INDEX IF NOT EXISTS idx_tickets_user_created ON support_tickets(telegram_id, created_at)']
        statements.append('CREATE TABLE IF NOT EXISTS physics_fact_state (telegram_id BIGINT PRIMARY KEY, position BIGINT NOT NULL DEFAULT 0 CHECK(position >= 0), pending_token TEXT, pending_until DOUBLE PRECISION NOT NULL DEFAULT 0, last_sent DOUBLE PRECISION NOT NULL DEFAULT 0)')
        statements.extend([
            'CREATE TABLE IF NOT EXISTS collection_records(collection TEXT NOT NULL, record_key TEXT NOT NULL, position BIGINT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(collection,record_key))',
            'CREATE TABLE IF NOT EXISTS content_progress(telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,content_id BIGINT NOT NULL REFERENCES content(id) ON DELETE CASCADE,views BIGINT NOT NULL DEFAULT 1,last_viewed TEXT NOT NULL,PRIMARY KEY(telegram_id,content_id))',
            'CREATE TABLE IF NOT EXISTS content_dates(id BIGINT PRIMARY KEY REFERENCES content(id) ON DELETE CASCADE,added_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS id_counters(name TEXT PRIMARY KEY,value BIGINT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS request_limits(user_id BIGINT NOT NULL,bucket TEXT NOT NULL,expires_at DOUBLE PRECISION NOT NULL,count BIGINT NOT NULL,PRIMARY KEY(user_id,bucket))',
            'CREATE INDEX IF NOT EXISTS idx_limits_expiry ON request_limits(expires_at)',
            'CREATE TABLE IF NOT EXISTS ai_daily(user_id BIGINT NOT NULL,day TEXT NOT NULL,tokens BIGINT NOT NULL,PRIMARY KEY(user_id,day))',
            'CREATE TABLE IF NOT EXISTS ai_reservations(token TEXT PRIMARY KEY,user_id BIGINT NOT NULL,day TEXT NOT NULL,tokens BIGINT NOT NULL,expires_at DOUBLE PRECISION NOT NULL)',
            'CREATE TABLE IF NOT EXISTS daily_physics(telegram_id BIGINT PRIMARY KEY,sent_date TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS delivery_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT \'pending\',attempts INTEGER NOT NULL DEFAULT 0,next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,updated_at DOUBLE PRECISION NOT NULL,error TEXT)',
            'CREATE INDEX IF NOT EXISTS idx_jobs_due ON delivery_jobs(state,next_attempt)',
        ])
        with self._lock:
            for statement in statements:
                self._execute(statement)
            self._connection.commit()

    def _migration_done(self, name):
        return bool(self._execute('SELECT 1 FROM schema_migrations WHERE name = ?', (name,)).fetchone())

    def _migration_mark(self, name):
        self._execute('INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)', (name, datetime.now(IRAQ_TZ).isoformat()))

    def _migrate_legacy_data(self):
        """Copy legacy JSON records once, transactionally, into normalized tables."""
        name = 'legacy_json_to_relational_v1'
        if self._migration_done(name):
            return
        try:
            with self._lock:
                users = {item.get('user_id'): item for item in self._read_data('users') if item.get('user_id')}
                stages = {item.get('user_id'): item for item in self._read_data('users_stages') if item.get('user_id')}
                now = datetime.now(IRAQ_TZ).isoformat()
                for uid, item in users.items():
                    self._execute('INSERT INTO users(telegram_id, username, first_name, last_name, joined_at, last_active, is_banned) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(telegram_id) DO NOTHING', (uid, item.get('username'), item.get('first_name'), item.get('last_name'), item.get('date_joined', now), item.get('last_active', now), int(bool(item.get('is_banned')))))
                for uid, item in stages.items():
                    if uid in users:
                        self._execute('INSERT INTO student_profiles(telegram_id, full_name, stage, registered_at) VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO NOTHING', (uid, item.get('full_name', ''), item.get('stage'), item.get('date_registered', now)))
                for item in self._read_data('subjects'):
                    self._execute('INSERT INTO subjects(stage, subject_key, name_ar, name_en, category) VALUES (?, ?, ?, ?, ?) ON CONFLICT(stage, subject_key) DO NOTHING', (item['stage'], item['key'], item['name_ar'], item.get('name_en'), item['category']))
                for item in self._read_data('stage_content'):
                    self._execute('INSERT INTO content(id, stage, subject_key, chapter, content_type, file_id, file_name, body, description, content_number, added_by, storage_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING', (item['id'], item['stage'], item['subject_key'], item['chapter_num'], item['content_type'], item.get('file_id'), item.get('file_name'), item.get('text'), item.get('description'), item.get('content_number', item['id']), item.get('added_by'), item.get('storage_message_id')))
                for item in self._read_data('support_tickets'):
                    if item.get('user_id') in users:
                        self._execute('INSERT INTO support_tickets(id, telegram_id, stage, message, created_at, status) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING', (item['id'], item['user_id'], item['stage'], item['message'], item.get('timestamp', now), item.get('status', 'open')))
                for item in self._read_data('comments'):
                    if item.get('user_id') in users:
                        self._execute('INSERT INTO comments(id, telegram_id, content_id, stage, body, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING', (item['id'], item['user_id'], item['content_id'], item.get('stage', 1), item['text'], item.get('timestamp', now)))
                self._migration_mark(name)
                self._connection.commit()
            logging.info('Completed relational migration %s', name)
        except Exception:
            self._connection.rollback()
            logging.exception('Relational migration failed; legacy app_data was left intact')
            raise

    def _connect_database(self):
        if self._backend == 'postgresql':
            import psycopg
            connection = psycopg.connect(self.database_url, connect_timeout=15)
            connection.execute("SELECT set_config('statement_timeout','180000',false)")
            connection.commit()
            return connection
        os.makedirs(os.path.dirname(os.path.abspath(self.database_path)), exist_ok=True)
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def _execute(self, query, params=()):
        if getattr(self._connection, 'closed', False):
            self._connection = self._connect_database()
        if self._backend == 'postgresql':
            query = query.replace('?', '%s')
        return self._connection.execute(query, params)

    def create_files(self):
        """تهيئة مجموعات البيانات داخل SQLite."""
        for key in self.data_keys:
            existing = self._execute('SELECT 1 FROM app_data WHERE key = ?', (key,)).fetchone()
            if existing:
                continue
            default = self._default_value(key)
            self._safe_write(key, default)

    def _default_value(self, key):
        if key in ('settings', 'comments_meta', 'ai_usage', 'notification_preferences'):
            return {}
        return []

    def insert_default_data(self):
        """إدخال البيانات الافتراضية (المواد، المطور، إعدادات الذكاء الاصطناعي)"""
        all_subjects = [(1, 'الرياضيات', 'mathematics', 'mathematics', 'subjects'), (1, 'اللغة العربية', 'arabic', 'arabic', 'subjects'), (1, 'اللغة الانجليزية', 'english', 'english', 'subjects'), (1, 'الحراره وخواص المادة', 'heat_material', 'heat_material', 'subjects'), (1, 'الكهربائية', 'electricity', 'electricity', 'subjects'), (1, 'اصول التربية', 'education_principles', 'education_principles', 'subjects'), (1, 'علم النفس', 'psychology', 'psychology', 'subjects'), (1, 'المكانيك', 'mechanics', 'mechanics', 'subjects'), (1, 'الحقوق والديمقراطية', 'rights_democracy', 'rights_democracy', 'subjects'), (1, 'الحاسوب', 'computer', 'computer', 'subjects'), (1, 'شرح الرياضيات', 'mathematics_exp', 'mathematics_exp', 'explanations'), (1, 'شرح اللغة العربية', 'arabic_exp', 'arabic_exp', 'explanations'), (1, 'شرح اللغة الانجليزية', 'english_exp', 'english_exp', 'explanations'), (1, 'شرح الحراره وخواص المادة', 'heat_material_exp', 'heat_material_exp', 'explanations'), (1, 'شرح الكهربائية', 'electricity_exp', 'electricity_exp', 'explanations'), (1, 'شرح اصول التربية', 'education_principles_exp', 'education_principles_exp', 'explanations'), (1, 'شرح علم النفس', 'psychology_exp', 'psychology_exp', 'explanations'), (1, 'شرح المكانيك', 'mechanics_exp', 'mechanics_exp', 'explanations'), (1, 'شرح الحقوق والديمقراطية', 'rights_democracy_exp', 'rights_democracy_exp', 'explanations'), (1, 'شرح الحاسوب', 'computer_exp', 'computer_exp', 'explanations'), (1, 'مختبر الميكانيك', 'mechanics_lab', 'mechanics_lab', 'lab'), (1, 'مختبر الكهربائية', 'electricity_lab', 'electricity_lab', 'lab'), (1, 'مختبر الحاسبات', 'computers_lab', 'computers_lab', 'lab'), (1, 'امتحان الرياضيات', 'mathematics_exam', 'mathematics_exam', 'exams'), (1, 'امتحان اللغة العربية', 'arabic_exam', 'arabic_exam', 'exams'), (1, 'امتحان اللغة الانجليزية', 'english_exam', 'english_exam', 'exams'), (1, 'امتحان الحراره وخواص المادة', 'heat_material_exam', 'heat_material_exam', 'exams'), (1, 'امتحان الكهربائية', 'electricity_exam', 'electricity_exam', 'exams'), (1, 'امتحان اصول التربية', 'education_principles_exam', 'education_principles_exam', 'exams'), (1, 'امتحان علم النفس', 'psychology_exam', 'psychology_exam', 'exams'), (1, 'امتحان المكانيك', 'mechanics_exam', 'mechanics_exam', 'exams'), (1, 'امتحان الحقوق والديمقراطية', 'rights_democracy_exam', 'rights_democracy_exam', 'exams'), (1, 'امتحان الحاسوب', 'computer_exam', 'computer_exam', 'exams'), (2, 'الرياضيات', 'mathematics', 'mathematics', 'subjects'), (2, 'الفلك', 'astronomy', 'astronomy', 'subjects'), (2, 'جرائم البعث', 'baath_crimes', 'baath_crimes', 'subjects'), (2, 'الصوت', 'sound', 'sound', 'subjects'), (2, 'الكهربائية', 'electricity', 'electricity', 'subjects'), (2, 'البصريات', 'optics', 'optics', 'subjects'), (2, 'الانكليزية', 'english', 'english', 'subjects'), (2, 'عربي', 'arabic', 'arabic', 'subjects'), (2, 'حاسبات', 'computer', 'computer', 'subjects'), (2, 'الادارة', 'management', 'management', 'subjects'), (2, 'تعليم التفكير', 'thinking_education', 'thinking_education', 'subjects'), (2, 'المنهج والبحث العلمي', 'curriculum_research', 'curriculum_research', 'subjects'), (2, 'شرح الرياضيات', 'mathematics_exp', 'mathematics_exp', 'explanations'), (2, 'شرح الفلك', 'astronomy_exp', 'astronomy_exp', 'explanations'), (2, 'شرح جرائم البعث', 'baath_crimes_exp', 'baath_crimes_exp', 'explanations'), (2, 'شرح الصوت', 'sound_exp', 'sound_exp', 'explanations'), (2, 'شرح الكهربائية', 'electricity_exp', 'electricity_exp', 'explanations'), (2, 'شرح البصريات', 'optics_exp', 'optics_exp', 'explanations'), (2, 'شرح الانكليزية', 'english_exp', 'english_exp', 'explanations'), (2, 'شرح عربي', 'arabic_exp', 'arabic_exp', 'explanations'), (2, 'شرح حاسبات', 'computer_exp', 'computer_exp', 'explanations'), (2, 'شرح الادارة', 'management_exp', 'management_exp', 'explanations'), (2, 'شرح تعليم التفكير', 'thinking_education_exp', 'thinking_education_exp', 'explanations'), (2, 'شرح المنهج والبحث العلمي', 'curriculum_research_exp', 'curriculum_research_exp', 'explanations'), (2, 'مختبر البرمجة', 'programming_lab', 'programming_lab', 'lab'), (2, 'مختبر الكهربائية', 'electricity_lab', 'electricity_lab', 'lab'), (2, 'مختبر البصريات', 'optics_lab', 'optics_lab', 'lab'), (2, 'امتحان الرياضيات', 'mathematics_exam', 'mathematics_exam', 'exams'), (2, 'امتحان الفلك', 'astronomy_exam', 'astronomy_exam', 'exams'), (2, 'امتحان جرائم البعث', 'baath_crimes_exam', 'baath_crimes_exam', 'exams'), (2, 'امتحان الصوت', 'sound_exam', 'sound_exam', 'exams'), (2, 'امتحان الكهربائية', 'electricity_exam', 'electricity_exam', 'exams'), (2, 'امتحان البصريات', 'optics_exam', 'optics_exam', 'exams'), (2, 'امتحان الانكليزية', 'english_exam', 'english_exam', 'exams'), (2, 'امتحان عربي', 'arabic_exam', 'arabic_exam', 'exams'), (2, 'امتحان حاسبات', 'computer_exam', 'computer_exam', 'exams'), (2, 'امتحان الادارة', 'management_exam', 'management_exam', 'exams'), (2, 'امتحان تعليم التفكير', 'thinking_education_exam', 'thinking_education_exam', 'exams'), (2, 'امتحان المنهج والبحث العلمي', 'curriculum_research_exam', 'curriculum_research_exam', 'exams'), (3, 'الدوال العقدية', 'complex_functions', 'complex_functions', 'subjects'), (3, 'الفيزياء الذرية', 'atomic_physics', 'atomic_physics', 'subjects'), (3, 'الالكترونيات', 'electronics', 'electronics', 'subjects'), (3, 'الثرموداينمك', 'thermodynamics', 'thermodynamics', 'subjects'), (3, 'مناهج وطرق التدريس', 'teaching_methods', 'teaching_methods', 'subjects'), (3, 'الارشاد التربوي', 'educational_guidance', 'educational_guidance', 'subjects'), (3, 'الميكانيك المتقدم', 'advanced_mechanics', 'advanced_mechanics', 'subjects'), (3, 'الانواء الجوية', 'meteorology', 'meteorology', 'subjects'), (3, 'شرح الدوال العقدية', 'complex_functions_exp', 'complex_functions_exp', 'explanations'), (3, 'شرح الفيزياء الذرية', 'atomic_physics_exp', 'atomic_physics_exp', 'explanations'), (3, 'شرح الالكترونيات', 'electronics_exp', 'electronics_exp', 'explanations'), (3, 'شرح الثرموداينمك', 'thermodynamics_exp', 'thermodynamics_exp', 'explanations'), (3, 'شرح مناهج وطرق التدريس', 'teaching_methods_exp', 'teaching_methods_exp', 'explanations'), (3, 'شرح الارشاد التربوي', 'educational_guidance_exp', 'educational_guidance_exp', 'explanations'), (3, 'شرح الميكانيك المتقدم', 'advanced_mechanics_exp', 'advanced_mechanics_exp', 'explanations'), (3, 'شرح الانواء الجوية', 'meteorology_exp', 'meteorology_exp', 'explanations'), (3, 'مختبر الالكترونيات', 'electronics_lab', 'electronics_lab', 'lab'), (3, 'مختبر الذرية', 'atomic_lab', 'atomic_lab', 'lab'), (3, 'امتحان الدوال العقدية', 'complex_functions_exam', 'complex_functions_exam', 'exams'), (3, 'امتحان الفيزياء الذرية', 'atomic_physics_exam', 'atomic_physics_exam', 'exams'), (3, 'امتحان الالكترونيات', 'electronics_exam', 'electronics_exam', 'exams'), (3, 'امتحان الثرموداينمك', 'thermodynamics_exam', 'thermodynamics_exam', 'exams'), (3, 'امتحان مناهج وطرق التدريس', 'teaching_methods_exam', 'teaching_methods_exam', 'exams'), (3, 'امتحان الارشاد التربوي', 'educational_guidance_exam', 'educational_guidance_exam', 'exams'), (3, 'امتحان الميكانيك المتقدم', 'advanced_mechanics_exam', 'advanced_mechanics_exam', 'exams'), (3, 'امتحان الانواء الجوية', 'meteorology_exam', 'meteorology_exam', 'exams'), (4, 'الميكانيك الكمي', 'quantum_mechanics', 'quantum_mechanics', 'subjects'), (4, 'الفيزياء الصلبة', 'solid_state_physics', 'solid_state_physics', 'subjects'), (4, 'الفيزياء النووية', 'nuclear_physics', 'nuclear_physics', 'subjects'), (4, 'الليزر', 'laser', 'laser', 'subjects'), (4, 'الكهرومغناطيسية', 'electromagnetism', 'electromagnetism', 'subjects'), (4, 'التربية العملية', 'practical_education', 'practical_education', 'subjects'), (4, 'القياس والتقويم', 'measurement_evaluation', 'measurement_evaluation', 'subjects'), (4, 'شرح الميكانيك الكمي', 'quantum_mechanics_exp', 'quantum_mechanics_exp', 'explanations'), (4, 'شرح الفيزياء الصلبة', 'solid_state_physics_exp', 'solid_state_physics_exp', 'explanations'), (4, 'شرح الفيزياء النووية', 'nuclear_physics_exp', 'nuclear_physics_exp', 'explanations'), (4, 'شرح الليزر', 'laser_exp', 'laser_exp', 'explanations'), (4, 'شرح الكهرومغناطيسية', 'electromagnetism_exp', 'electromagnetism_exp', 'explanations'), (4, 'شرح التربية العملية', 'practical_education_exp', 'practical_education_exp', 'explanations'), (4, 'شرح القياس والتقويم', 'measurement_evaluation_exp', 'measurement_evaluation_exp', 'explanations'), (4, 'مختبر الميكانيك الكمي', 'quantum_mechanics_lab', 'quantum_mechanics_lab', 'lab'), (4, 'مختبر الفيزياء النووية', 'nuclear_physics_lab', 'nuclear_physics_lab', 'lab'), (4, 'مختبر الليزر', 'laser_lab', 'laser_lab', 'lab'), (4, 'امتحان الميكانيك الكمي', 'quantum_mechanics_exam', 'quantum_mechanics_exam', 'exams'), (4, 'امتحان الفيزياء الصلبة', 'solid_state_physics_exam', 'solid_state_physics_exam', 'exams'), (4, 'امتحان الفيزياء النووية', 'nuclear_physics_exam', 'nuclear_physics_exam', 'exams'), (4, 'امتحان الليزر', 'laser_exam', 'laser_exam', 'exams'), (4, 'امتحان الكهرومغناطيسية', 'electromagnetism_exam', 'electromagnetism_exam', 'exams'), (4, 'امتحان التربية العملية', 'practical_education_exam', 'practical_education_exam', 'exams'), (4, 'امتحان القياس والتقويم', 'measurement_evaluation_exam', 'measurement_evaluation_exam', 'exams'), (4, 'البحث', 'physics_research', 'physics_research', 'research'), (4, 'المشاهدة', 'mathematics_research', 'mathematics_research', 'research'), (4, 'التطبيق ', 'computer_research', 'computer_research', 'research')]
        exam_subjects = [subject for subject in all_subjects if subject[4] == 'exams']
        all_subjects = [subject for subject in all_subjects if subject[4] != 'exams']
        for stage, name_ar, name_en, key, _ in exam_subjects:
            base_key = key[:-5] if key.endswith('_exam') else key
            all_subjects.extend([(stage, name_ar, f'{name_en}_monthly', f'{base_key}_monthly_exam', 'monthly_exams'), (stage, name_ar, f'{name_en}_final', f'{base_key}_final_exam', 'final_exams')])
        subjects_data = []
        for subject in all_subjects:
            subjects_data.append({'stage': subject[0], 'name_ar': subject[1], 'name_en': subject[2], 'key': subject[3], 'category': subject[4]})
        all_subjects = [x for x in all_subjects if not (x[0] == 4 and x[4] == 'lab' and (x[3] != 'nuclear_physics_lab'))]
        subjects_data = [x for x in subjects_data if not (x['stage'] == 4 and x['category'] == 'lab' and (x['key'] != 'nuclear_physics_lab'))]
        current_subjects = self._read_data('subjects')
        if not current_subjects:
            self._write_data('subjects', subjects_data)
        else:
            for subject in current_subjects:
                if subject['stage'] == 4 and subject['category'] == 'lab' and (subject['key'] != 'nuclear_physics_lab'):
                    subject['category'] = 'archived_lab'
            nuclear = next((x for x in current_subjects if x['stage'] == 4 and x['key'] == 'nuclear_physics_lab'), None)
            if not nuclear:
                current_subjects.append({'stage': 4, 'name_ar': 'مختبر النووية', 'name_en': 'nuclear_physics_lab', 'key': 'nuclear_physics_lab', 'category': 'lab'})
            else:
                nuclear['name_ar'] = 'مختبر النووية'
                nuclear['category'] = 'lab'
            self._write_data('subjects', current_subjects)
        admins = self._read_data('admins')
        if not any((a.get('user_id') == DEVELOPER_ID for a in admins)):
            admins.append({'user_id': DEVELOPER_ID, 'username': None, 'full_name': 'المطور', 'date_added': datetime.now().isoformat(), 'added_by': DEVELOPER_ID})
            self._write_data('admins', admins)

    def _read_data(self, key):
        if self._data_ready:
            records = self._execute('SELECT record_key,payload FROM collection_records WHERE collection=? ORDER BY position,record_key', (key,)).fetchall()
            if key in self.ROWS:
                table, keys, fields = self.ROWS[key]
                extras = {ident:json.loads(payload) for ident,payload in records}
                result = []
                for values in self._execute('SELECT '+','.join(fields.values())+' FROM '+table).fetchall():
                    item = dict(zip(fields,values))
                    if key == 'quizzes': item['options'] = json.loads(item['options'])
                    for name in ('is_banned','enabled','correct'):
                        if name in item: item[name] = bool(item[name])
                    item.update(extras.get(self._record_key(key,item,0),{}))
                    result.append(item)
                return result
            if isinstance(self._default_value(key),dict):
                return {ident:json.loads(payload) for ident,payload in records}
            return [json.loads(payload) for _,payload in records]
        row = self._execute('SELECT value FROM app_data WHERE key=?',(key,)).fetchone()
        if not row: return self._default_value(key)
        try:
            value = json.loads(row[0])
        except (TypeError,JSONDecodeError) as error:
            raise RuntimeError('Corrupt stored collection: '+key) from error
        if not isinstance(value,type(self._default_value(key))):
            raise RuntimeError('Invalid stored collection: '+key)
        return value

    def _safe_write(self, key, data, retries=3, delay=0.1):
        previous = self._read_data(key)
        if not self._data_ready:
            self._execute('INSERT INTO app_data(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,json.dumps(data,ensure_ascii=False),datetime.now(IRAQ_TZ).isoformat()))
        else:
            existing = {r[0]:(r[1],r[2]) for r in self._execute('SELECT record_key,position,payload FROM collection_records WHERE collection=?',(key,)).fetchall()}
            items = data.items() if isinstance(data,dict) else enumerate(data)
            current = {}
            for position,(index,row) in enumerate(items):
                ident = str(index) if isinstance(data,dict) else self._record_key(key,row,index)
                payload = ({k:v for k,v in row.items() if k not in self.ROWS[key][2]} if key in self.ROWS else row)
                current[ident] = (position,json.dumps(payload,ensure_ascii=False))
            for ident in existing.keys()-current.keys():
                self._execute('DELETE FROM collection_records WHERE collection=? AND record_key=?',(key,ident))
            for ident,(position,payload) in current.items():
                if existing.get(ident) != (position,payload):
                    self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,?,?) ON CONFLICT(collection,record_key) DO UPDATE SET position=excluded.position,payload=excluded.payload',(key,ident,position,payload))
        if getattr(self,'_projections_ready',False): self._sync_projection(key,previous,data)
        return True

    def _write_data(self,key,data):
        return self._safe_write(key,data)

    def _mutate_data(self, key, mutation):
        """Atomically read, alter and persist one JSON collection.

        The application deliberately keeps its legacy JSON-in-one-table format.
        Holding the same re-entrant lock across the whole operation prevents a
        second local request from overwriting a concurrent update.
        """
        with self._lock:
            data = self._read_data(key)
            result = mutation(data)
            self._write_data(key, data)
            return result

    def create_backup(self):
        from backup_tools import postgres_environment, run_tool, write_manifest, check_backup_versions, BackupToolError
        os.makedirs(self.backup_path,exist_ok=True)
        stamp = datetime.now(IRAQ_TZ).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
        suffix = '.dump' if self._backend == 'postgresql' else '.db'
        target = os.path.join(self.backup_path,'bot4stage_'+stamp+suffix)
        partial = target+'.partial'
        try:
            if self._backend == 'postgresql':
                if not shutil.which('pg_dump') or not shutil.which('pg_restore'):
                    raise BackupToolError('missing_tool', 'أدوات PostgreSQL غير مثبتة؛ أعد النشر باستخدام Dockerfile المرفق.')
                with postgres_environment(self.database_url) as (environment,database):
                    check_backup_versions(self.get_postgres_server_version(), environment)
                    run_tool(['pg_dump','--dbname',database,'--format=custom','--file',partial,'--no-owner','--no-acl'],environment)
                    run_tool(['pg_restore','--list',partial],environment)
            else:
                with self._lock:
                    destination = sqlite3.connect(partial)
                    try:
                        self._initial_connection.backup(destination)
                        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise RuntimeError('SQLite backup integrity check failed')
                    finally: destination.close()
            if not os.path.isfile(partial) or not os.path.getsize(partial):
                raise RuntimeError('Empty backup')
            os.replace(partial,target)
            os.chmod(target,0o600)
            write_manifest(target)
            # Pruning follows confirmed external delivery, not local creation.
            return target
        finally:
            if os.path.exists(partial): os.unlink(partial)

    def confirm_backup_delivery(self,path,message_id,destination):
        from backup_tools import validate_manifest
        metadata = validate_manifest(path)
        metadata.update(remote_confirmed=True,message_id=message_id,destination=destination,confirmed_at=datetime.now(IRAQ_TZ).isoformat())
        with open(path+'.json','w') as manifest: json.dump(metadata,manifest,indent=2)
        self._prune_backups(os.path.splitext(path)[1])

    def _prune_backups(self,suffix,keep=7):
        files = sorted((os.path.join(self.backup_path,n) for n in os.listdir(self.backup_path) if n.endswith(suffix)),key=os.path.getmtime)
        confirmed = []
        for path in files:
            try:
                from backup_tools import validate_manifest
                if validate_manifest(path).get('remote_confirmed'): confirmed.append(path)
            except (OSError,ValueError): continue
        for path in confirmed[:-keep]:
            os.unlink(path)
            os.unlink(path+'.json')

    def restore_postgres_backup(self,backup_path):
        from backup_tools import postgres_environment,run_tool,validate_manifest
        if self._backend != 'postgresql': raise RuntimeError('PostgreSQL restore only')
        if self._worker_claimed: raise RuntimeError('Stop the bot worker before restoring')
        if not os.path.isfile(backup_path) or not backup_path.endswith('.dump'): raise ValueError('Invalid backup')
        validate_manifest(backup_path)
        import psycopg
        with psycopg.connect(self.database_url,autocommit=True,connect_timeout=15) as gate:
            if not gate.execute('SELECT pg_try_advisory_lock(42831005)').fetchone()[0]:
                raise RuntimeError('Another bot worker is active; stop it before restoring')
            with postgres_environment(self.database_url) as (environment,database):
                run_tool(['pg_restore','--list',backup_path],environment)
                run_tool(['pg_restore','--dbname',database,'--clean','--if-exists','--single-transaction','--exit-on-error','--no-owner','--no-acl',backup_path],environment)

    def _put_row(self,key,row):
        if not self._data_ready:
            items=self._read_data(key)
            ident=self._record_key(key,row,0)
            items=[item for index,item in enumerate(items) if self._record_key(key,item,index)!=ident]
            items.append(row);self._write_data(key,items);return
        self._sync_projection(key,[],[row])
        ident=self._record_key(key,row,0)
        extras={k:v for k,v in row.items() if k not in self.ROWS[key][2]}
        self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,?,?) ON CONFLICT(collection,record_key) DO UPDATE SET payload=excluded.payload',(key,ident,int(row.get('id',row.get('user_id',0))),json.dumps(extras,ensure_ascii=False)))
        if key=='stage_content':
            self._execute('INSERT INTO content_dates(id,added_at) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET added_at=excluded.added_at',(row['id'],row.get('date_added',datetime.now(IRAQ_TZ).isoformat())))

    def get_comments_page(self,content_id,offset=0,limit=21):
        return [tuple(row) for row in self._execute('SELECT c.id,c.telegram_id,c.body,c.created_at,u.first_name,u.username FROM comments c JOIN users u ON u.telegram_id=c.telegram_id WHERE content_id=? ORDER BY c.created_at,c.id LIMIT ? OFFSET ?',(content_id,min(101,max(1,limit)),max(0,offset))).fetchall()]

    def add_user(self,user_id,username,first_name,last_name):
        if not self._data_ready:
            users=self._read_data('users');existing=next((u for u in users if u['user_id']==user_id),None)
        else:
            existing=self._get_user_by_id(user_id)
        now=datetime.now(IRAQ_TZ).isoformat()
        old=self._execute('SELECT joined_at FROM users WHERE telegram_id=?',(user_id,)).fetchone()
        row={'user_id':user_id,'username':username,'first_name':first_name,'last_name':last_name,'date_joined':old[0] if old else now,'last_active':now,'is_new':not bool(existing),'is_banned':bool(existing and existing.get('is_banned'))}
        self._put_row('users',row)
        return True

    def get_user_stage(self, user_id):
        row = self._execute('SELECT stage,full_name FROM student_profiles WHERE telegram_id=?', (user_id,)).fetchone()
        return tuple(row) if row else None

    def set_user_stage(self,user_id,full_name,stage):
        if type(stage) is not int or stage not in (1,2,3,4) or not 3<=len(full_name.strip())<=120 or len(full_name.split())<2: raise ValueError('Invalid registration')
        if self.get_user_stage(user_id): raise ValueError('Already registered')
        self._put_row('users_stages',{'user_id':user_id,'full_name':full_name.strip(),'stage':stage,'date_registered':datetime.now(IRAQ_TZ).isoformat()})
        self._execute('UPDATE users SET last_active=? WHERE telegram_id=?',(datetime.now(IRAQ_TZ).isoformat(),user_id))
        ident=json.dumps([user_id],separators=(',',':'))
        extra=self._execute('SELECT payload FROM collection_records WHERE collection=? AND record_key=?',('users',ident)).fetchone()
        if extra:
            value=json.loads(extra[0]);value['is_new']=False
            self._execute('UPDATE collection_records SET payload=? WHERE collection=? AND record_key=?',(json.dumps(value,ensure_ascii=False),'users',ident))
        return True

    def update_user_activity(self,user_id):
        now=time.monotonic()
        if now-self._activity_times.get(user_id,-1000)<300:return
        self._execute('UPDATE users SET last_active=? WHERE telegram_id=?',(datetime.now(IRAQ_TZ).isoformat(),user_id))
        self._activity_times[user_id]=now
        if len(self._activity_times)>10000:self._activity_times={k:v for k,v in self._activity_times.items() if now-v<300}

    def get_all_users(self):
        return [row[0] for row in self._execute('SELECT telegram_id FROM users WHERE is_banned=0').fetchall()]

    def get_new_users(self):
        users = self._read_data('users')
        new_users = []
        for user in users:
            if user.get('is_new', False) and (not user.get('is_banned', False)):
                new_users.append((user['user_id'], user.get('first_name', ''), user.get('last_name', ''), user.get('username', '')))
        return new_users

    def count_users(self):
        return self._execute('SELECT count(*) FROM users WHERE is_banned=0').fetchone()[0]

    def is_banned(self, user_id):
        row = self._execute('SELECT is_banned FROM users WHERE telegram_id=?', (user_id,)).fetchone()
        return bool(row and row[0])

    def ban_user(self, user_id):
        users = self._read_data('users')
        for user in users:
            if user['user_id'] == user_id:
                user['is_banned'] = True
                self._write_data('users', users)
                return True
        return False

    def unban_user(self, user_id):
        users = self._read_data('users')
        for user in users:
            if user['user_id'] == user_id:
                user['is_banned'] = False
                self._write_data('users', users)
                return True
        return False

    def is_admin(self, user_id):
        admins = self._read_data('admins')
        for admin in admins:
            if admin['user_id'] == user_id:
                return True
        return False

    def add_admin(self, user_id, username, full_name, added_by):
        admins = self._read_data('admins')
        for admin in admins:
            if admin['user_id'] == user_id:
                return False
        admins.append({'user_id': user_id, 'username': username, 'full_name': full_name, 'date_added': datetime.now().isoformat(), 'added_by': added_by})
        self._write_data('admins', admins)
        return True

    def remove_admin(self, user_id):
        if user_id == DEVELOPER_ID:
            return False
        admins = self._read_data('admins')
        for i, admin in enumerate(admins):
            if admin['user_id'] == user_id:
                del admins[i]
                self._write_data('admins', admins)
                settings = self._read_data('settings')
                if isinstance(settings, dict):
                    support_admins = settings.get('support_admins', {})
                    settings['support_admins'] = {stage: admin_id for stage, admin_id in support_admins.items() if admin_id != user_id}
                    self._write_data('settings', settings)
                return True
        return False

    def get_admins(self):
        admins = self._read_data('admins')
        return [(admin['user_id'], admin.get('username'), admin.get('full_name')) for admin in admins]

    def set_support_admin(self, stage, admin_id):
        """تعيين أدمن واحد لاستلام تذاكر الدعم لكل مرحلة."""
        if stage not in (1, 2, 3, 4) or not self.is_admin(admin_id):
            return False
        settings = self._read_data('settings')
        if not isinstance(settings, dict):
            settings = {}
        support_admins = settings.get('support_admins', {})
        support_admins[str(stage)] = admin_id
        settings['support_admins'] = support_admins
        self._write_data('settings', settings)
        return True

    def get_support_admin(self, stage):
        settings = self._read_data('settings')
        if not isinstance(settings, dict):
            return None
        admin_id = settings.get('support_admins', {}).get(str(stage))
        return int(admin_id) if admin_id else None

    def get_stage_subjects_by_category(self,stage,category):
        return [tuple(row) for row in self._execute('SELECT name_ar,subject_key FROM subjects WHERE stage=? AND category=? ORDER BY name_ar',(stage,category)).fetchall()]

    def get_stage_subject(self,stage,subject_key):
        row = self._execute('SELECT name_ar,name_en,subject_key,category FROM subjects WHERE stage=? AND subject_key=?',(stage,subject_key)).fetchone()
        return tuple(row) if row else None

    def get_required_channel(self):
        """إرجاع أول قناة للتوافق مع الاستدعاءات القديمة."""
        channels = self.get_required_channels()
        if not channels:
            return None
        _, channel_id, username, title = channels[0]
        return (channel_id, username, title)

    def get_required_channels(self):
        channels = self._read_data('required_channels')
        return [(channel.get('id', index), channel['channel_id'], channel['channel_username'], channel['channel_title']) for index, channel in enumerate(channels, start=1)]

    def add_required_channel(self, channel_id, channel_username, channel_title, added_by):
        channels = self._read_data('required_channels')
        if any((str(channel.get('channel_id')) == str(channel_id) for channel in channels)):
            return False
        new_id = max([channel.get('id', index) for index, channel in enumerate(channels, start=1)], default=0) + 1
        channels.append({'id': new_id, 'channel_id': channel_id, 'channel_username': channel_username, 'channel_title': channel_title, 'date_added': datetime.now().isoformat(), 'added_by': added_by})
        self._write_data('required_channels', channels)
        return True

    def remove_required_channel(self, record_id):
        channels = self._read_data('required_channels')
        for index, channel in enumerate(channels):
            if channel.get('id', index + 1) == record_id:
                del channels[index]
                self._write_data('required_channels', channels)
                return True
        return False

    def add_search_channel(self, channel_id, channel_username, channel_title, stage, added_by):
        channels = self._read_data('search_channels')
        for channel in channels:
            if channel['channel_id'] == channel_id and channel['stage'] == stage:
                return False
        new_id = max([c.get('id', 0) for c in channels], default=0) + 1
        channels.append({'id': new_id, 'channel_id': channel_id, 'channel_username': channel_username, 'channel_title': channel_title, 'stage': stage, 'added_by': added_by, 'date_added': datetime.now().isoformat()})
        self._write_data('search_channels', channels)
        return True

    def get_search_channels(self, stage=None):
        channels = self._read_data('search_channels')
        if stage is None:
            return [(channel['id'], channel['channel_username'], channel['channel_title'], channel['stage']) for channel in channels]
        else:
            return [(channel['id'], channel['channel_username'], channel['channel_title'], channel['stage']) for channel in channels if channel['stage'] == stage]

    def remove_search_channel(self, record_id, stage=None):
        channels = self._read_data('search_channels')
        for i, channel in enumerate(channels):
            if channel['id'] == record_id:
                if stage is None or channel['stage'] == stage:
                    del channels[i]
                    self._write_data('search_channels', channels)
                    return True
        return False

    def get_search_channel_by_id(self, record_id):
        channels = self._read_data('search_channels')
        for channel in channels:
            if channel['id'] == record_id:
                return channel
        return None

    def add_support_ticket(self, user_id, message, stage):
        profile = self.get_user_stage(user_id)
        if not profile or profile[0] != stage or self.is_banned(user_id) or not 1<=len(message.strip())<=2000: raise ValueError('Invalid ticket')
        tickets = self._read_data('support_tickets')
        ticket_id = self._next_id('support_tickets', tickets)
        tickets.append({'id': ticket_id, 'user_id': user_id, 'message': message, 'stage': stage, 'date': datetime.now().isoformat()})
        self._write_data('support_tickets', tickets)
        recipients = {DEVELOPER_ID}
        support_admin = self.get_support_admin(stage)
        if support_admin: recipients.add(support_admin)
        for admin in recipients:
            self.enqueue_job(f'support:{ticket_id}:{admin}','support',{'ticket_id':ticket_id,'user_id':admin})
        return ticket_id

    def get_support_tickets(self):
        tickets = self._read_data('support_tickets')
        result = []
        for ticket in tickets:
            if ticket.get('status','open') != 'open': continue
            user = self._get_user_by_id(ticket['user_id'])
            if user:
                result.append((ticket['id'], ticket['message'], ticket['stage'], user.get('first_name', ''), user.get('username', ''), ticket['date']))
        result.sort(key=lambda x: x[5], reverse=True)
        return result

    def get_ticket_record(self,ticket_id):
        row=self._execute('SELECT id,telegram_id,stage,message,created_at,status FROM support_tickets WHERE id=?',(ticket_id,)).fetchone()
        if not row:return None
        item=dict(zip(('id','user_id','stage','message','date','status'),row))
        extra=self._execute('SELECT payload FROM collection_records WHERE collection=? AND record_key=?',('support_tickets',json.dumps([ticket_id],separators=(',',':')))).fetchone()
        if extra:item.update(json.loads(extra[0]))
        return item

    def get_ticket_info(self, ticket_id):
        tickets = self._read_data('support_tickets')
        for ticket in tickets:
            if ticket['id'] == ticket_id:
                user = self._get_user_by_id(ticket['user_id'])
                if user:
                    return (ticket['user_id'], ticket['message'], ticket['stage'], user.get('first_name', ''), user.get('username', ''), ticket['date'])
        return None

    def delete_support_ticket(self, ticket_id):
        tickets = self._read_data('support_tickets')
        for i, ticket in enumerate(tickets):
            if ticket['id'] == ticket_id:
                del tickets[i]
                self._write_data('support_tickets', tickets)
                return True
        return False

    def _get_user_by_id(self,user_id):
        row=self._execute('SELECT telegram_id,username,first_name,last_name,is_banned FROM users WHERE telegram_id=?',(user_id,)).fetchone()
        return dict(zip(('user_id','username','first_name','last_name','is_banned'),row)) if row else None

    def add_stage_content(self,stage,subject_key,chapter_num,content_type,file_id,file_name=None,text=None,description=None,added_by=None,storage_message_id=None):
        if not self.is_valid_chapter(stage,subject_key,chapter_num): raise ValueError('Invalid chapter')
        if added_by is not None and not self.can_manage_stage(added_by,stage):raise PermissionError('Wrong admin stage')
        number=self._execute('SELECT COALESCE(MAX(content_number),0)+1 FROM content WHERE stage=? AND subject_key=? AND chapter=?',(stage,subject_key,chapter_num)).fetchone()[0]
        cid=self._next_id('stage_content',[])
        self._put_row('stage_content',{'id':cid,'stage':stage,'subject_key':subject_key,'chapter_num':chapter_num,'content_type':content_type,'file_id':file_id,'file_name':file_name,'text':text,'description':description,'content_number':number,'added_by':added_by,'storage_message_id':storage_message_id,'date_added':datetime.now(IRAQ_TZ).isoformat()})
        return cid

    def get_stage_content(self,stage,subject_key,chapter_num):
        return [tuple(row) for row in self._execute("SELECT c.id,c.content_type,c.description,COALESCE(d.added_at,''),c.content_number FROM content c LEFT JOIN content_dates d ON d.id=c.id WHERE c.stage=? AND c.subject_key=? AND c.chapter=? ORDER BY c.content_number",(stage,subject_key,chapter_num)).fetchall()]

    def get_stage_content_by_id(self, content_id):
        row = self._execute('SELECT id,stage,subject_key,chapter,content_type,file_id,file_name,body,description,added_by,content_number FROM content WHERE id=?', (content_id,)).fetchone()
        return tuple(row) if row else None

    def get_content_storage_message_id(self,content_id):
        row=self._execute('SELECT storage_message_id FROM content WHERE id=?',(content_id,)).fetchone()
        return row[0] if row else None

    def update_content_description(self,content_id,description):
        return bool(self._execute('UPDATE content SET description=? WHERE id=?',(description,content_id)).rowcount)

    def delete_stage_content(self, content_id, actor=None):
        content=self.get_stage_content_by_id(content_id)
        if actor is not None and (not content or not self.can_manage_stage(actor,content[1])):raise PermissionError("Wrong admin stage")
        contents = self._read_data('stage_content')
        if not any((c['id'] == content_id for c in contents)):
            return False
        for key in ('comments', 'favorites', 'progress', 'scheduled_content'):
            self._write_data(key, [x for x in self._read_data(key) if x.get('content_id') != content_id])
        meta = self._read_data('comments_meta')
        meta.pop(str(content_id), None)
        self._write_data('comments_meta', meta)
        self._write_data('stage_content', [c for c in contents if c['id'] != content_id])
        return True

    def get_all_comments(self):
        comments = self._read_data('comments')
        result = []
        for comment in comments:
            user = self._get_user_by_id(comment['user_id'])
            if user:
                result.append((comment['id'], comment['user_id'], comment['text'], comment['date'], user.get('first_name', ''), user.get('username', ''), comment.get('stage', 1)))
        result.sort(key=lambda x: x[3], reverse=True)
        return result

    def can_user_comment(self,user_id,min_interval_seconds=10):
        row=self._execute('SELECT created_at FROM comments WHERE telegram_id=? ORDER BY created_at DESC LIMIT 1',(user_id,)).fetchone()
        if not row:return True
        last=datetime.fromisoformat(row[0])
        if last.tzinfo is None:last=last.replace(tzinfo=IRAQ_TZ)
        return (datetime.now(IRAQ_TZ)-last).total_seconds()>=min_interval_seconds

    def add_comment(self,user_id,content_id,text,stage=None):
        content=self.get_stage_content_by_id(content_id);profile=self.get_user_stage(user_id)
        if not content or not profile or content[1]!=profile[0] or not 1<=len(text.strip())<=1000 or self.is_banned(user_id) or not self.can_user_comment(user_id):return None
        cid=self._next_id('comments',[])
        self._put_row('comments',{'id':cid,'user_id':user_id,'content_id':content_id,'stage':profile[0],'text':text.strip(),'date':datetime.now(IRAQ_TZ).isoformat()})
        return cid

    def get_comments_for_content(self,content_id):
        return [tuple(row) for row in self._execute('SELECT c.id,c.telegram_id,c.body,c.created_at,u.first_name,u.username FROM comments c JOIN users u ON u.telegram_id=c.telegram_id WHERE content_id=? ORDER BY c.created_at,c.id',(content_id,)).fetchall()]

    def delete_comment(self, comment_id, requestor_id=None):
        comments = self._read_data('comments')
        for i, c in enumerate(comments):
            if c['id'] == comment_id:
                if requestor_id is None or requestor_id == c['user_id'] or self.can_manage_stage(requestor_id,c.get('stage')):
                    del comments[i]
                    self._write_data('comments', comments)
                    meta = self._read_data('comments_meta') or {}
                    key = str(c.get('content_id'))
                    if key in meta:
                        meta[key]['count'] = max(0, meta[key].get('count', 1) - 1)
                        self._write_data('comments_meta', meta)
                    return True
        return False

    def set_ai_enabled(self, enabled):
        """تفعيل أو تعطيل الذكاء الاصطناعي"""
        settings = self._read_data('settings')
        if not isinstance(settings, dict):
            settings = {}
        settings['ai_enabled'] = enabled
        settings['ai_updated'] = datetime.now().isoformat()
        self._write_data('settings', settings)
        return True

    def get_ai_enabled(self):
        """الحصول على حالة الذكاء الاصطناعي"""
        settings = self._read_data('settings')
        if isinstance(settings, dict):
            return settings.get('ai_enabled', False)
        return False

    def set_physics_info_channel(self, channel_id, channel_username, channel_title, added_by):
        """تعيين قناة المعلومات الفيزيائية"""
        channels = [{'channel_id': channel_id, 'channel_username': channel_username, 'channel_title': channel_title, 'added_by': added_by, 'date_added': datetime.now().isoformat()}]
        self._write_data('physics_channel', channels)
        return True

    def get_physics_info_channel(self):
        """الحصول على قناة المعلومات الفيزيائية"""
        channels = self._read_data('physics_channel')
        if channels and len(channels) > 0:
            channel = channels[0]
            return (channel['channel_id'], channel['channel_username'], channel['channel_title'])
        return None

    def remove_physics_info_channel(self):
        """إزالة قناة المعلومات الفيزيائية"""
        self._write_data('physics_channel', [])
        return True

    def add_user_physics_info_request(self, user_id, timestamp=None):
        """تسجيل طلب معلومة فيزيائية من المستخدم"""
        requests = self._read_data('physics_requests')
        if not isinstance(requests, list):
            requests = []
        requests.append({'user_id': user_id, 'timestamp': timestamp or datetime.now().isoformat(), 'delivered': False})
        self._write_data('physics_requests', requests)
        return True

    def reserve_physics_fact(self, user_id):
        """Persist a send reservation; unsuccessful sends do not consume a fact."""
        from physics_facts import fact_for_position
        import uuid
        user_id = int(user_id)
        self._execute('INSERT INTO physics_fact_state(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING', (user_id,))
        position, token, until = self._execute('SELECT position, pending_token, pending_until FROM physics_fact_state WHERE telegram_id=?', (user_id,)).fetchone()
        now = time.time()
        if token and until > now:
            return None
        fact = dict(fact_for_position(user_id, position))
        token = uuid.uuid4().hex
        self._execute('UPDATE physics_fact_state SET pending_token=?, pending_until=? WHERE telegram_id=?', (token, now + 300, user_id))
        return {'token': token, 'fact': fact}

    def complete_physics_fact(self, user_id, token):
        result = self._execute('UPDATE physics_fact_state SET position=position+1, pending_token=NULL, pending_until=0, last_sent=? WHERE telegram_id=? AND pending_token=?', (time.time(), int(user_id), token))
        return result.rowcount == 1

    def cancel_physics_fact(self, user_id, token):
        self._execute('UPDATE physics_fact_state SET pending_token=NULL, pending_until=0 WHERE telegram_id=? AND pending_token=?', (int(user_id), token))

    def get_users_eligible_for_physics_info(self, hours_since=24):
        """Daily eligibility includes preferences, bans, and successful local sends."""
        last_sent = {int(uid): stamp for uid, stamp in self._execute('SELECT telegram_id,last_sent FROM physics_fact_state').fetchall()}
        # Respect successful legacy channel deliveries during the transition.
        for request in self._read_data('physics_requests') or []:
            try:
                uid = int(request['user_id'])
                stamp = datetime.fromisoformat(request['timestamp'])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=IRAQ_TZ)
                last_sent[uid] = max(last_sent.get(uid, 0), stamp.timestamp())
            except (ValueError, TypeError, KeyError):
                continue
        now = time.time()
        return [profile['user_id'] for profile in self._read_data('users_stages')
                if not self.is_banned(profile['user_id'])
                and self.notifications_enabled(profile['user_id']) and not self.is_delivery_blocked(profile['user_id'])
                and now - last_sent.get(int(profile['user_id']), 0) >= hours_since * 3600]

    def count_users_by_stage(self, stage=None):
        """عدد المستخدمين حسب المرحلة"""
        users_stages = self._read_data('users_stages')
        if stage:
            return len([u for u in users_stages if u['stage'] == stage])
        else:
            return len(users_stages)

    def get_users_by_stage(self, stage=None):
        """الحصول على قائمة المستخدمين حسب المرحلة"""
        users_stages = self._read_data('users_stages')
        users = self._read_data('users')
        users_dict = {u['user_id']: u for u in users}
        result = []
        for user_stage in users_stages:
            if stage is None or user_stage['stage'] == stage:
                user_id = user_stage['user_id']
                user_info = users_dict.get(user_id, {})
                result.append({'user_id': user_id, 'full_name': user_stage.get('full_name', ''), 'stage': user_stage['stage'], 'username': user_info.get('username', ''), 'first_name': user_info.get('first_name', ''), 'last_name': user_info.get('last_name', ''), 'date_joined': user_info.get('date_joined', ''), 'last_active': user_info.get('last_active', ''), 'is_banned': user_info.get('is_banned', False)})
        result.sort(key=lambda x: x.get('date_joined', ''), reverse=True)
        return result

    def get_stage_statistics(self):
        """إحصائيات كاملة عن المراحل"""
        users_stages = self._read_data('users_stages')
        users = self._read_data('users')
        stages_count = {1: 0, 2: 0, 3: 0, 4: 0}
        for user_stage in users_stages:
            stage = user_stage['stage']
            if stage in stages_count:
                stages_count[stage] += 1
        total_users = len([u for u in users if not u.get('is_banned', False)])
        today = date.today().isoformat()
        new_users_today = len([u for u in users if u.get('date_joined', '').startswith(today) and (not u.get('is_banned', False))])
        active_today = len([u for u in users if u.get('last_active', '').startswith(today) and (not u.get('is_banned', False))])
        return {'total_users': total_users, 'new_users_today': new_users_today, 'active_today': active_today, 'stage_1': stages_count[1], 'stage_2': stages_count[2], 'stage_3': stages_count[3], 'stage_4': stages_count[4], 'total_registered': sum(stages_count.values())}

    def toggle_favorite(self,user_id,content_id):
        if self.is_favorite(user_id,content_id):
            self._execute('DELETE FROM favorites WHERE telegram_id=? AND content_id=?',(user_id,content_id))
            self._execute('DELETE FROM collection_records WHERE collection=? AND record_key=?',('favorites',json.dumps([user_id,content_id],separators=(',',':'))))
            return False
        self._put_row('favorites',{'user_id':user_id,'content_id':content_id,'created_at':datetime.now(IRAQ_TZ).isoformat()})
        return True

    def is_favorite(self,user_id,content_id):
        return bool(self._execute('SELECT 1 FROM favorites WHERE telegram_id=? AND content_id=?',(user_id,content_id)).fetchone())

    def get_user_favorites(self,user_id):
        return [tuple(row) for row in self._execute('SELECT c.id,c.stage,c.subject_key,c.chapter,c.content_type,c.file_id,c.file_name,c.body,c.description,c.added_by,c.content_number FROM content c JOIN favorites f ON f.content_id=c.id WHERE f.telegram_id=? ORDER BY f.created_at DESC',(user_id,)).fetchall()]

    def mark_content_viewed(self,user_id,content_id):
        self._execute('INSERT INTO content_progress(telegram_id,content_id,views,last_viewed) VALUES (?,?,1,?) ON CONFLICT(telegram_id,content_id) DO UPDATE SET views=content_progress.views+1,last_viewed=excluded.last_viewed',(user_id,content_id,datetime.now(IRAQ_TZ).isoformat()))

    def get_user_progress(self,user_id):
        viewed,total=self._execute('SELECT COUNT(*),COALESCE(SUM(views),0) FROM content_progress WHERE telegram_id=?',(user_id,)).fetchone()
        attempts,correct=self._execute('SELECT COUNT(*),COALESCE(SUM(is_correct),0) FROM quiz_attempts WHERE telegram_id=?',(user_id,)).fetchone()
        return {'viewed':viewed,'total_views':total,'quiz_attempts':attempts,'quiz_correct':correct,'quiz_percent':round(correct*100/attempts) if attempts else 0}

    def search_content(self, query, stage=None, limit=20, offset=0):
        terms = [x for x in str(query).split() if len(x) > 1]
        if not terms:
            return []
        clauses = ["s.category != 'archived_lab'"]
        params = []
        if stage is not None:
            clauses.append('c.stage=?')
            params.append(stage)
        operator = 'ILIKE' if self._backend == 'postgresql' else 'LIKE'
        for term in terms:
            clauses.append(f'(c.description {operator} ? OR c.body {operator} ? OR c.file_name {operator} ? OR s.name_ar {operator} ?)')
            params.extend(['%' + term + '%'] * 4)
        sql = 'SELECT c.id,c.stage,c.subject_key,c.chapter,c.content_type,c.file_id,c.file_name,c.body,c.description,c.content_number,c.added_by,s.name_ar FROM content c JOIN subjects s ON s.stage=c.stage AND s.subject_key=c.subject_key WHERE ' + ' AND '.join(clauses) + ' ORDER BY c.id DESC'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params.extend([max(1, int(limit)), max(0, int(offset))])
        rows = self._execute(sql, params).fetchall()
        return [({'id': r[0], 'stage': r[1], 'subject_key': r[2], 'chapter_num': r[3], 'content_type': r[4], 'file_id': r[5], 'file_name': r[6], 'text': r[7], 'description': r[8], 'content_number': r[9], 'added_by': r[10]}, r[11]) for r in rows]

    def add_quiz(self,stage,subject_key,question,options,correct_index,added_by,question_type='multiple_choice',accepted_answers=None):
        from quiz_tools import KINDS,normalize_answer
        if not self.can_manage_stage(added_by,stage):raise PermissionError('Admin only')
        if question_type not in KINDS or type(stage) is not int or stage not in (1,2,3,4) or not self.get_stage_subject(stage,subject_key) or not isinstance(question,str) or not 1<=len(question.strip())<=1000:raise ValueError('Invalid quiz')
        answers=[]
        if question_type=='fill_blank':
            if not isinstance(accepted_answers,list) or not 1<=len(accepted_answers)<=10 or any(not isinstance(x,str) or not 1<=len(x.strip())<=100 or not normalize_answer(x) for x in accepted_answers):raise ValueError('Invalid accepted answers')
            answers=list(dict.fromkeys(x.strip() for x in accepted_answers));options=[];correct_index=0
        else:
            if question_type=='true_false':options=['صح','خطأ']
            if not isinstance(options,list) or not 2<=len(options)<=6 or any(not isinstance(x,str) or not 1<=len(x.strip())<=100 for x in options) or len(set(options))!=len(options) or type(correct_index) is not int or correct_index not in range(len(options)):raise ValueError('Invalid quiz options')
        quiz_id=self._next_id('quizzes',[])
        self._put_row('quizzes',{'id':quiz_id,'stage':stage,'subject_key':subject_key,'question':question.strip(),'options':options,'correct_index':correct_index,'added_by':added_by,'created_at':datetime.now(IRAQ_TZ).isoformat(),'enabled':True,'question_type':question_type,'accepted_answers':answers})
        return quiz_id

    def get_manageable_quizzes(self,actor,stage,offset=0,limit=6):
        if not self.can_manage_stage(actor,stage):raise PermissionError('Wrong admin stage')
        return [self.get_quiz(row[0]) for row in self._execute('SELECT id FROM quizzes WHERE enabled=1 AND stage=? ORDER BY id DESC LIMIT ? OFFSET ?',(stage,min(21,max(1,int(limit))),max(0,int(offset)))).fetchall()]

    def delete_quiz(self,quiz_id,actor):
        quiz=self.get_quiz(quiz_id)
        if not quiz:return False
        if not self.can_manage_stage(actor,quiz['stage']):raise PermissionError('Wrong admin stage')
        if not quiz['enabled']:return False
        quiz.update(enabled=False,deleted_by=actor,deleted_at=datetime.now(IRAQ_TZ).isoformat())
        self._put_row('quizzes',quiz)
        self.log_action(actor,'quiz_deleted',{'quiz_id':quiz_id,'stage':quiz['stage']})
        return True

    def get_random_quiz(self,stage,user_id=None):
        row=self._execute('SELECT q.id FROM quizzes q WHERE q.stage=? AND q.enabled=1 AND NOT EXISTS(SELECT 1 FROM quiz_attempts a WHERE a.quiz_id=q.id AND a.telegram_id=?) ORDER BY RANDOM() LIMIT 1',(stage,user_id)).fetchone()
        quiz=self.get_quiz(row[0]) if row else None
        if quiz and user_id is not None:
            settings=self._read_data('settings');offers=settings.setdefault('quiz_offers',{})
            now=time.time();offers={key:value for key,value in offers.items() if now-value.get('at',0)<900}
            offers[str(user_id)]={'quiz_id':quiz['id'],'at':now};settings['quiz_offers']=offers;self._write_data('settings',settings)
        return quiz

    def get_quiz(self,quiz_id):
        row=self._execute('SELECT id,stage,subject_key,question,options_json,correct_index,enabled,added_by,created_at FROM quizzes WHERE id=?',(quiz_id,)).fetchone()
        if not row:return None
        item=dict(zip(('id','stage','subject_key','question','options','correct_index','enabled','added_by','created_at'),row));item['options']=json.loads(item['options']);item['enabled']=bool(item['enabled'])
        extras=self._execute('SELECT payload FROM collection_records WHERE collection=? AND record_key=?',('quizzes',json.dumps([quiz_id],separators=(',',':')))).fetchone()
        if extras:
            values=json.loads(extras[0]);item.update({key:values[key] for key in ('question_type','accepted_answers','deleted_by','deleted_at') if key in values})
        item.setdefault('question_type','multiple_choice');item.setdefault('accepted_answers',[])
        return item

    def record_quiz_attempt(self,user_id,quiz_id,answer_index):
        from quiz_tools import normalize_answer
        quiz=self.get_quiz(quiz_id);profile=self.get_user_stage(user_id)
        if not quiz or not quiz['enabled'] or not profile or quiz['stage']!=profile[0] or self.is_banned(user_id):return None
        blank=quiz.get('question_type')=='fill_blank'
        if blank:
            if not isinstance(answer_index,str) or not 1<=len(answer_index.strip())<=100:return None
            correct=normalize_answer(answer_index) in {normalize_answer(x) for x in quiz['accepted_answers']}
            stored_index=-1;answer_text=answer_index.strip()
        else:
            if type(answer_index) is not int or answer_index not in range(len(quiz['options'])):return None
            correct=answer_index==quiz['correct_index'];stored_index=answer_index;answer_text=None
        existing=self._execute('SELECT is_correct FROM quiz_attempts WHERE telegram_id=? AND quiz_id=?',(user_id,quiz_id)).fetchone()
        if existing:return bool(existing[0])
        offer=self._read_data('settings').get('quiz_offers',{}).get(str(user_id),{})
        if offer.get('quiz_id')!=quiz_id or time.time()-offer.get('at',0)>900:return None
        self._put_row('quiz_attempts',{'id':self._next_id('quiz_attempts',[]),'user_id':user_id,'quiz_id':quiz_id,'answer_index':stored_index,'answer_text':answer_text,'correct':correct,'created_at':datetime.now(IRAQ_TZ).isoformat()})
        return correct

    def get_postgres_server_version(self):
        return self._connection.info.server_version

    def get_notification_preferences(self, user_id):
        if not self._data_ready:
            return self._read_data('notification_preferences').get(str(user_id), {})
        row = self._execute('SELECT payload FROM collection_records WHERE collection=? AND record_key=?', ('notification_preferences', str(user_id))).fetchone()
        return json.loads(row[0]) if row else {}

    def notifications_enabled(self, user_id):
        return self.get_notification_preferences(user_id).get('enabled', True)

    def is_delivery_blocked(self, user_id):
        return bool(self.get_notification_preferences(user_id).get('delivery_blocked', False))

    def set_delivery_blocked(self, user_id, blocked=True, reason='unavailable', occurred_at=None, event_order=None):
        if reason not in ('blocked', 'deactivated', 'unavailable'): raise ValueError('Invalid delivery reason')
        value = self.get_notification_preferences(user_id)
        if event_order is not None and int(event_order) <= value.get('delivery_event_order', -1): return False
        now = datetime.now(IRAQ_TZ)
        state_at = float(occurred_at) if occurred_at is not None else now.timestamp()
        if occurred_at is not None and state_at < value.get('delivery_state_at', 0): return False
        if event_order is not None: value['delivery_event_order'] = int(event_order)
        previous = bool(value.get('delivery_blocked', False))
        old_reason = value.get('delivery_block_reason', 'unavailable')
        old_detected_at = value.get('delivery_blocked_at') or value.get('delivery_last_blocked_at') or value.get('delivery_changed_at')
        if previous == bool(blocked) and (not blocked or reason == old_reason):
            if event_order is None and (occurred_at is None or state_at <= value.get('delivery_state_at', 0)): return False
            value['delivery_state_at'] = state_at
            self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,0,?) ON CONFLICT(collection,record_key) DO UPDATE SET payload=excluded.payload', ('notification_preferences', str(user_id), json.dumps(value)))
            return False
        stamp = now.isoformat()
        value.update(delivery_blocked=bool(blocked), delivery_changed_at=stamp, delivery_state_at=state_at)
        if blocked:
            if not previous:
                value['delivery_blocked_at'] = stamp
            value.setdefault('delivery_blocked_at', old_detected_at or stamp)
            value['delivery_last_blocked_at'] = value['delivery_blocked_at']
            value['delivery_block_reason'] = reason
            value['delivery_last_block_reason'] = reason
            if occurred_at is not None:
                value['delivery_action_at'] = datetime.fromtimestamp(state_at, IRAQ_TZ).isoformat()
            elif not previous:
                value.pop('delivery_action_at', None)
        else:
            value.setdefault('delivery_last_blocked_at', old_detected_at or stamp)
            value.setdefault('delivery_last_block_reason', old_reason)
            value['delivery_unblocked_at'] = stamp
            value['delivery_block_reason'] = None
        self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,0,?) ON CONFLICT(collection,record_key) DO UPDATE SET payload=excluded.payload', ('notification_preferences', str(user_id), json.dumps(value)))
        return True

    def get_bot_block_report(self, actor, category='blocked', page=0, page_size=8):
        if actor != DEVELOPER_ID or self.is_banned(actor): raise PermissionError('Developer only')
        if category not in ('blocked', 'deactivated', 'unavailable', 'returned'): raise ValueError('Invalid report category')
        page_size = min(10, max(1, int(page_size)))
        rows = self._execute('SELECT r.record_key,r.payload,u.username,u.first_name,u.last_name,p.full_name,p.stage FROM collection_records r LEFT JOIN users u ON r.record_key=CAST(u.telegram_id AS TEXT) LEFT JOIN student_profiles p ON r.record_key=CAST(p.telegram_id AS TEXT) WHERE r.collection=?', ('notification_preferences',)).fetchall()
        groups = {key: [] for key in ('blocked', 'deactivated', 'unavailable', 'returned')}
        for key, payload, username, first, last, full_name, stage in rows:
            try: uid = int(key)
            except (TypeError, ValueError): continue
            if uid <= 0: continue
            state = json.loads(payload)
            active = bool(state.get('delivery_blocked', False))
            reason = state.get('delivery_block_reason') or 'unavailable'
            if not active and not state.get('delivery_last_blocked_at'): continue
            group = reason if active and reason in groups and reason != 'returned' else 'unavailable' if active else 'returned'
            item = {'user_id': uid, 'username': username, 'name': full_name or ' '.join(x for x in (first, last) if x) or 'اسم غير مسجل', 'stage': stage,
                    'reason': reason if active else state.get('delivery_last_block_reason', 'unavailable'),
                    'detected_at': state.get('delivery_blocked_at') or state.get('delivery_last_blocked_at') or state.get('delivery_changed_at'),
                    'action_at': state.get('delivery_action_at'), 'returned_at': state.get('delivery_unblocked_at')}
            groups[group].append(item)
        counts = {key: len(items) for key, items in groups.items()}
        selected = groups[category]
        selected.sort(key=lambda x: (x.get('returned_at') if category == 'returned' else x.get('detected_at')) or '', reverse=True)
        pages = max(1, (len(selected) + page_size - 1) // page_size)
        page = min(max(0, int(page)), pages - 1)
        return {'items': selected[page*page_size:(page+1)*page_size], 'counts': counts, 'total': len(selected), 'page': page, 'pages': pages, 'category': category}

    def toggle_notifications(self, user_id):
        value = self.get_notification_preferences(user_id)
        enabled = not value.get('enabled', True)
        value['enabled'] = enabled
        self._execute('INSERT INTO collection_records(collection,record_key,position,payload) VALUES (?,?,0,?) ON CONFLICT(collection,record_key) DO UPDATE SET payload=excluded.payload', ('notification_preferences', str(user_id), json.dumps(value)))
        return enabled

    def schedule_content(self, content_id, publish_at, added_by):
        when = datetime.fromisoformat(publish_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=IRAQ_TZ)
        content=self.get_stage_content_by_id(content_id)
        if not content or not self.can_manage_stage(added_by,content[1]):raise PermissionError('Wrong admin stage')
        if when <= datetime.now(IRAQ_TZ) or not content:
            raise ValueError('Invalid schedule')
        scheduled = self._read_data('scheduled_content')
        schedule_id = self._next_id('scheduled_content', scheduled)
        scheduled.append({'id': schedule_id, 'content_id': content_id, 'publish_at': publish_at, 'added_by': added_by, 'sent': False})
        self._write_data('scheduled_content', scheduled)
        return schedule_id

    def get_due_scheduled_content(self):
        scheduled = self._read_data('scheduled_content')
        now = datetime.now(IRAQ_TZ)
        due = []
        for item in scheduled:
            if item.get('sent') or item.get('queued'):
                continue
            try:
                publish_at = datetime.fromisoformat(item['publish_at'])
                if publish_at.tzinfo is None:
                    publish_at = publish_at.replace(tzinfo=IRAQ_TZ)
                if publish_at <= now:
                    due.append(item)
            except (TypeError, ValueError):
                logging.warning('Ignoring scheduled item with invalid date: %s', item.get('id'))
        return due

    def mark_schedule_sent(self, schedule_id):
        scheduled = self._read_data('scheduled_content')
        for item in scheduled:
            if item['id'] == schedule_id:
                item['sent'] = True
                item['sent_at'] = datetime.now().isoformat()
                self._write_data('scheduled_content', scheduled)
                return True
        return False

    def log_action(self, user_id, action, details=None):
        entries = self._read_data('audit_log')
        entries.append({'user_id': user_id, 'action': action, 'details': details or {}, 'created_at': datetime.now().isoformat()})
        self._write_data('audit_log', entries[-5000:])

    def get_learning_statistics(self):
        progress = self._read_data('progress')
        attempts = self._read_data('quiz_attempts')
        return {'favorites': len(self._read_data('favorites')), 'unique_viewers': len({x['user_id'] for x in progress}), 'content_views': sum((x.get('views', 0) for x in progress)), 'quizzes': len(self._read_data('quizzes')), 'quiz_attempts': len(attempts), 'correct_answers': sum((1 for x in attempts if x.get('correct')))}

    def get_quiz_leaderboard(self, stage=None, limit=10):
        attempts = self._read_data('quiz_attempts')
        quizzes = {x['id']: x for x in self._read_data('quizzes')}
        scores = {}
        for attempt in attempts:
            quiz = quizzes.get(attempt['quiz_id'])
            if not quiz or (stage and quiz.get('stage') != stage):
                continue
            score = scores.setdefault(attempt['user_id'], {'correct': 0, 'attempts': 0})
            score['attempts'] += 1
            score['correct'] += int(bool(attempt.get('correct')))
        rows = []
        for user_id, score in scores.items():
            user_stage = self.get_user_stage(user_id)
            name = user_stage[1] if user_stage else str(user_id)
            rows.append((user_id, name, score['correct'], score['attempts']))
        rows.sort(key=lambda x: (x[2], x[2] / x[3]), reverse=True)
        return rows[:limit]

    def _upgrade_chapter_schema(self):
        """Remove the obsolete six-chapter constraint without removing content."""
        if self._backend == 'postgresql':
            constraints = self._execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='content'::regclass AND contype='c'").fetchall()
            for name, definition in constraints:
                if 'chapter' in definition and ('<= 6' in definition or 'BETWEEN' in definition):
                    from psycopg import sql
                    self._connection.execute(sql.SQL('ALTER TABLE content DROP CONSTRAINT {}').format(sql.Identifier(name)))
                    self._execute('ALTER TABLE content ADD CONSTRAINT content_chapter_positive CHECK(chapter>0)')
            self._connection.commit()
            return
        row = self._execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='content'").fetchone()
        if not row or 'BETWEEN 1 AND 6' not in row[0]:
            return
        self._connection.commit()
        self._connection.execute('PRAGMA foreign_keys=OFF')
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            import re
            sql = re.sub('^CREATE TABLE\\s+(?:"content"|`content`|\\[content\\]|content)(?=\\s|\\()', 'CREATE TABLE content_upgrade', row[0], count=1, flags=re.I).replace('CHECK(chapter BETWEEN 1 AND 6)', 'CHECK(chapter > 0)')
            self._connection.execute(sql)
            self._connection.execute('INSERT INTO content_upgrade SELECT * FROM content')
            self._connection.execute('DROP TABLE content')
            self._connection.execute('ALTER TABLE content_upgrade RENAME TO content')
            self._connection.execute('CREATE INDEX idx_content_stage_subject_chapter ON content(stage,subject_key,chapter)')
            self._connection.execute('CREATE INDEX idx_content_search ON content(stage,description)')
            if self._connection.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('Foreign-key validation failed during chapter migration')
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            self._connection.execute('PRAGMA foreign_keys=ON')

    def lab_experiment_count(self, stage):
        if stage not in (1, 2, 3, 4):
            raise ValueError('Invalid stage')
        return 4 if stage == 4 else 6

    def lab_slot(self, stage, course, experiment):
        count = self.lab_experiment_count(stage)
        if course not in (1, 2) or experiment not in range(1, count + 1):
            raise ValueError('Invalid experiment')
        return (course - 1) * count + experiment

    def get_chapters(self, stage, subject_key):
        subject = self.get_stage_subject(stage, subject_key)
        if not subject or subject[3] == 'archived_lab':
            return []
        if subject[3] == 'lab':
            count = self.lab_experiment_count(stage)
            return [{'id': self.lab_slot(stage, course, exp), 'label': f'الكورس {course} · التجربة {exp}', 'course': course, 'experiment': exp} for course in (1, 2) for exp in range(1, count + 1)]
        stored = self._read_data('settings').get('subject_chapters', {}).get(f'{stage}:{subject_key}')
        if stored is not None:
            return stored
        count = 10 if stage == 4 and subject_key in ('laser', 'laser_exp') else 1 if subject[3] in ('monthly_exams', 'final_exams', 'research') or subject_key == 'practical_education' else 6
        return [{'id': i, 'label': f'الفصل {i}'} for i in range(1, count + 1)]

    def is_valid_chapter(self, stage, subject_key, chapter):
        return isinstance(chapter, int) and (not isinstance(chapter, bool)) and any((x['id'] == chapter for x in self.get_chapters(stage, subject_key)))

    def chapter_label(self, stage, subject_key, chapter):
        row = next((x for x in self.get_chapters(stage, subject_key) if x['id'] == chapter), None)
        return row['label'] if row else f'قسم محفوظ #{chapter}'

    def add_chapter(self, stage, subject_key, label, actor):
        if actor != DEVELOPER_ID:
            raise PermissionError('Developer only')
        subject = self.get_stage_subject(stage, subject_key)
        if not subject or subject[3] in ('lab', 'archived_lab') or (not isinstance(label, str)) or (not 1 <= len(label.strip()) <= 80):
            raise ValueError('Invalid chapter')
        chapters = self.get_chapters(stage, subject_key)
        settings = self._read_data('settings')
        key = f'{stage}:{subject_key}'
        counters = settings.setdefault('chapter_sequences', {})
        content_ids = [x['chapter_num'] for x in self._read_data('stage_content') if x['stage'] == stage and x['subject_key'] == subject_key]
        chapter = max(counters.get(key, 0), max([x['id'] for x in chapters] + content_ids + [0])) + 1
        if chapter > 2147483647:
            raise ValueError('Chapter limit')
        counters[key] = chapter
        chapters.append({'id': chapter, 'label': label.strip()})
        settings.setdefault('subject_chapters', {})[key] = chapters
        self._write_data('settings', settings)
        self.log_action(actor, 'chapter_added', {'stage': stage, 'subject': subject_key, 'chapter': chapter})
        return chapter

    def remove_chapter(self, stage, subject_key, chapter, actor):
        if actor != DEVELOPER_ID:
            raise PermissionError('Developer only')
        subject = self.get_stage_subject(stage, subject_key)
        if not subject or subject[3] in ('lab', 'archived_lab'):
            raise ValueError('Invalid chapter')
        chapters = self.get_chapters(stage, subject_key)
        if not any((x['id'] == chapter for x in chapters)):
            return False
        settings = self._read_data('settings')
        key = f'{stage}:{subject_key}'
        settings.setdefault('chapter_sequences', {})[key] = max(settings.get('chapter_sequences', {}).get(key, 0), max((x['id'] for x in chapters)))
        settings.setdefault('subject_chapters', {})[key] = [x for x in chapters if x['id'] != chapter]
        self._write_data('settings', settings)
        self.log_action(actor, 'chapter_button_removed', {'stage': stage, 'subject': subject_key, 'chapter': chapter})
        return True

    def complete_quiz_draft(self, actor, index):
        if not self.is_admin(actor) or self.is_banned(actor):
            raise PermissionError('Admin only')
        pending = self.get_workflow(actor, 'pending')
        if not pending or pending.get('action') != 'quiz_correct':
            raise ValueError('Quiz draft expired')
        data = pending['data']
        quiz_id = self.add_quiz(data['stage'],data['key'],data['question'],data.get('options',[]),index,actor,data.get('question_type','multiple_choice'),data.get('accepted_answers'))
        self.set_workflow(actor, 'pending', None)
        self.log_action(actor, 'quiz_added', {'quiz_id': quiz_id})
        return quiz_id

    def finish_upload(self, user_id, **payload):
        state = self.get_workflow(user_id, 'upload')
        if not state or state.get('step') != 'content' or (not self.is_admin(user_id)) or self.is_banned(user_id):
            raise PermissionError('Upload expired or forbidden')
        if not self.can_manage_stage(user_id,payload.get('stage')): raise PermissionError('Wrong admin stage')
        for field in ('stage', 'subject_key', 'content_type'):
            if payload.get(field) != state.get(field):
                raise ValueError('Upload target changed')
        if payload.get('chapter_num') != state.get('chapter_num'):
            raise ValueError('Upload chapter changed')
        cid = self.add_stage_content(**payload)
        for uid, in self._execute('SELECT telegram_id FROM student_profiles WHERE stage=?',(payload['stage'],)).fetchall():
            if self.is_delivery_blocked(uid): continue
            self.enqueue_job(f'content:{cid}:{uid}','content',{'content_id':cid,'user_id':uid})
        self.set_workflow(user_id, 'upload', None)
        return cid

    def claim_worker(self):
        if self._worker_claimed:
            return True
        if self._backend == 'postgresql':
            if self._lease_connection is None or self._lease_connection.closed:
                import psycopg
                self._lease_connection = psycopg.connect(self.database_url,autocommit=True,connect_timeout=15,keepalives=1,keepalives_idle=10,keepalives_interval=5,keepalives_count=3)
                self._lease_connection.execute('SET statement_timeout=5000')
            if not self._lease_connection.execute('SELECT pg_try_advisory_lock(42831005)').fetchone()[0]:
                return False
        else:
            handle = open(self.database_path + '.worker.lock', 'a+b')
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    handle.write(b'0')
                    handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return False
            self._worker_file = handle
        self._worker_claimed = True
        return True

    def get_workflow(self, user_id, kind):
        self._execute('DELETE FROM workflow_state WHERE expires_at < ?', (time.time(),))
        row = self._execute('SELECT payload FROM workflow_state WHERE telegram_id=? AND kind=?', (user_id, kind)).fetchone()
        return json.loads(row[0]) if row else None

    def set_workflow(self, user_id, kind, value):
        if value is None:
            self._execute('DELETE FROM workflow_state WHERE telegram_id=? AND kind=?', (user_id, kind))
            return
        self._execute('INSERT INTO workflow_state(telegram_id,kind,payload,expires_at) VALUES (?,?,?,?) ON CONFLICT(telegram_id,kind) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at', (user_id, kind, json.dumps(value, ensure_ascii=False), time.time() + 900))

    def workflow_users(self, kind):
        return [x[0] for x in self._execute('SELECT telegram_id FROM workflow_state WHERE kind=? AND expires_at>?', (kind, time.time())).fetchall()]

    def rate_limit(self, user_id, bucket, count=30, period=60):
        now = time.time()
        self._execute('DELETE FROM request_limits WHERE expires_at<?',(now,))
        row = self._execute('SELECT expires_at,count FROM request_limits WHERE user_id=? AND bucket=?',(user_id,bucket)).fetchone()
        expires, used = row if row else (now+period,0)
        if used >= count: return False
        self._execute('INSERT INTO request_limits(user_id,bucket,expires_at,count) VALUES (?,?,?,?) ON CONFLICT(user_id,bucket) DO UPDATE SET expires_at=excluded.expires_at,count=excluded.count',(user_id,bucket,expires,used+1))
        return True

    def _next_id(self, key, rows):
        maximum = max((int(x.get('id',0)) for x in rows),default=0)
        self._execute('INSERT INTO id_counters(name,value) VALUES (?,?) ON CONFLICT(name) DO NOTHING',(key,maximum))
        return self._execute('UPDATE id_counters SET value=CASE WHEN value>? THEN value+1 ELSE ? END WHERE name=? RETURNING value',(maximum,maximum+1,key)).fetchone()[0]

    def ping(self):
        return self._execute('SELECT 1').fetchone()[0] == 1

    def close(self):
        if self._pool:
            self._pool.close()
        self._initial_connection.close()
        if self._lease_connection:
            self._lease_connection.close()
        if self._worker_file:
            self._worker_file.close()
        self._worker_claimed = False

    def _repair_projections(self):
        self._projections_ready = False
        with self._lock:
            self._transaction_depth = 1
            try:
                if self._backend == 'postgresql':
                    self._execute('SELECT pg_advisory_xact_lock(42831004)')
                if self._migration_done('authoritative_rows_v2'):
                    self._data_ready=True;self._projections_ready=True
                    self._connection.commit();return
                users = self._read_data('users')
                known = {u['user_id'] for u in users}
                for key in ('users_stages', 'comments', 'support_tickets', 'favorites', 'quiz_attempts'):
                    for row in self._read_data(key):
                        uid = row.get('user_id')
                        if uid and uid not in known:
                            users.append({'user_id': uid, 'first_name': '', 'last_name': '', 'username': None, 'date_joined': datetime.now(IRAQ_TZ).isoformat(), 'last_active': datetime.now(IRAQ_TZ).isoformat(), 'is_banned': False})
                            known.add(uid)
                self._safe_write('users', users)
                attempts = []
                seen = set()
                for item in self._read_data('quiz_attempts'):
                    pair = (item['user_id'], item['quiz_id'])
                    if pair in seen:
                        continue
                    seen.add(pair)
                    item['id'] = len(attempts) + 1
                    attempts.append(item)
                self._safe_write('quiz_attempts', attempts)
                valid = {x['id'] for x in self._read_data('stage_content')}
                for key in ('comments', 'favorites', 'progress', 'scheduled_content'):
                    self._safe_write(key, [x for x in self._read_data(key) if x.get('content_id') in valid])
                self._projections_ready = True
                for key in ('users', 'users_stages', 'subjects', 'stage_content', 'support_tickets', 'comments', 'favorites','progress', 'quizzes', 'quiz_attempts'):
                    self._sync_projection(key, [], self._read_data(key), repair=True)
                settings = self._read_data('settings')
                seq = settings.setdefault('sequences', {})
                for key in ('stage_content', 'comments', 'support_tickets', 'quizzes', 'quiz_attempts', 'scheduled_content'):
                    seq[key] = max(seq.get(key, 0), max((x.get('id', 0) for x in self._read_data(key)), default=0))
                self._safe_write('settings', settings)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                self._transaction_depth = 0

    def _sync_projection(self, key, old, new, repair=False):
        now = datetime.now(IRAQ_TZ).isoformat()
        specs = {'users': ('users', ('telegram_id',), ('telegram_id', 'username', 'first_name', 'last_name', 'joined_at', 'last_active', 'is_banned'), lambda x: (x['user_id'], x.get('username'), x.get('first_name'), x.get('last_name'), x.get('date_joined', now), x.get('last_active', now), int(x.get('is_banned', False)))), 'users_stages': ('student_profiles', ('telegram_id',), ('telegram_id', 'full_name', 'stage', 'registered_at'), lambda x: (x['user_id'], x['full_name'], x['stage'], x.get('date_registered', now))), 'subjects': ('subjects', ('stage', 'subject_key'), ('stage', 'subject_key', 'name_ar', 'name_en', 'category'), lambda x: (x['stage'], x['key'], x['name_ar'], x.get('name_en'), x['category'])), 'stage_content': ('content', ('id',), ('id', 'stage', 'subject_key', 'chapter', 'content_type', 'file_id', 'file_name', 'body', 'description', 'content_number', 'added_by', 'storage_message_id'), lambda x: (x['id'], x['stage'], x['subject_key'], x['chapter_num'], x['content_type'], x.get('file_id'), x.get('file_name'), x.get('text'), x.get('description'), x['content_number'], x.get('added_by'), x.get('storage_message_id'))), 'support_tickets': ('support_tickets', ('id',), ('id', 'telegram_id', 'stage', 'message', 'created_at', 'status'), lambda x: (x['id'], x['user_id'], x['stage'], x['message'], x.get('date', now), x.get('status', 'open'))), 'comments': ('comments', ('id',), ('id', 'telegram_id', 'content_id', 'stage', 'body', 'created_at'), lambda x: (x['id'], x['user_id'], x['content_id'], x['stage'], x['text'], x.get('date', now))), 'favorites': ('favorites', ('telegram_id', 'content_id'), ('telegram_id', 'content_id', 'created_at'), lambda x: (x['user_id'], x['content_id'], x.get('created_at', now))), 'quizzes': ('quizzes', ('id',), ('id', 'stage', 'subject_key', 'question', 'options_json', 'correct_index', 'enabled', 'added_by', 'created_at'), lambda x: (x['id'], x['stage'], x['subject_key'], x['question'], json.dumps(x['options']), x['correct_index'], int(x.get('enabled', True)), x.get('added_by'), x.get('created_at', now))), 'quiz_attempts': ('quiz_attempts', ('id',), ('id', 'telegram_id', 'quiz_id', 'answer_index', 'is_correct', 'attempted_at'), lambda x: (x['id'], x['user_id'], x['quiz_id'], x['answer_index'], int(x['correct']), x.get('created_at', now)))}
        specs['progress']=('content_progress',('telegram_id','content_id'),('telegram_id','content_id','views','last_viewed'),lambda x:(x['user_id'],x['content_id'],x.get('views',1),x.get('last_viewed',now)))
        if key not in specs:
            return
        table, pk, cols, convert = specs[key]
        indices = [cols.index(k) for k in pk]
        oldrows = {tuple((row[i] for i in indices)): row for row in map(convert, old)}
        newrows = {tuple((row[i] for i in indices)): row for row in map(convert, new)}
        if repair:
            oldrows = {tuple(row): None for row in self._execute('SELECT ' + ','.join(pk) + ' FROM ' + table).fetchall()}
        for ident in oldrows.keys() - newrows.keys():
            self._execute('DELETE FROM ' + table + ' WHERE ' + ' AND '.join((k + ' = ?' for k in pk)), ident)
        update = ','.join((k + '=excluded.' + k for k in cols if k not in pk))
        query = 'INSERT INTO ' + table + '(' + ','.join(cols) + ') VALUES (' + ','.join(('?' for _ in cols)) + ') ON CONFLICT(' + ','.join(pk) + ') DO UPDATE SET ' + update
        for ident, row in newrows.items():
            if row != oldrows.get(ident):
                self._execute(query, row)

    def get_ai_daily_usage(self,user_id):
        row=self._execute('SELECT tokens FROM ai_daily WHERE user_id=? AND day=?',(user_id,datetime.now(IRAQ_TZ).date().isoformat())).fetchone()
        return row[0] if row else 0

    def add_ai_daily_usage(self,user_id,tokens):
        self._execute('INSERT INTO ai_daily(user_id,day,tokens) VALUES (?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET tokens=ai_daily.tokens+excluded.tokens',(user_id,datetime.now(IRAQ_TZ).date().isoformat(),max(0,int(tokens))))

    def reserve_ai(self,user_id,tokens,limit):
        if tokens<=0:return None
        today=datetime.now(IRAQ_TZ).date().isoformat();now=time.time()
        self._execute('DELETE FROM ai_reservations WHERE expires_at<?',(now,))
        used=self._execute('SELECT COALESCE(SUM(tokens),0) FROM ai_daily WHERE user_id=? AND day=?',(user_id,today)).fetchone()[0]
        reserved=self._execute('SELECT COALESCE(SUM(tokens),0) FROM ai_reservations WHERE user_id=? AND day=?',(user_id,today)).fetchone()[0]
        total=self._execute('SELECT COALESCE(SUM(tokens),0) FROM ai_daily WHERE day=?',(today,)).fetchone()[0]
        pending,active=self._execute('SELECT COALESCE(SUM(tokens),0),COUNT(*) FROM ai_reservations WHERE day=?',(today,)).fetchone()
        try:
            global_limit=max(1,int(os.getenv('OPENAI_GLOBAL_DAILY_LIMIT','40000')))
            parallel=max(1,int(os.getenv('OPENAI_MAX_CONCURRENT','5')))
        except ValueError:raise ValueError('Invalid AI budget configuration') from None
        if used+reserved+tokens>limit or total+pending+tokens>global_limit or active>=parallel:return None
        token=uuid.uuid4().hex
        self._execute('INSERT INTO ai_reservations(token,user_id,day,tokens,expires_at) VALUES (?,?,?,?,?)',(token,user_id,today,tokens,now+180))
        return token

    def settle_ai(self,user_id,reservation,actual):
        row=self._execute('SELECT user_id,day FROM ai_reservations WHERE token=?',(reservation,)).fetchone()
        if not row or row[0]!=user_id:return False
        self._execute('DELETE FROM ai_reservations WHERE token=?',(reservation,))
        self._execute('INSERT INTO ai_daily(user_id,day,tokens) VALUES (?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET tokens=ai_daily.tokens+excluded.tokens',(user_id,row[1],max(0,int(actual))))
        self._execute('DELETE FROM ai_daily WHERE day<?',((datetime.now(IRAQ_TZ).date()-timedelta(days=30)).isoformat(),))
        return True

    def queue_schedule(self,schedule_id):
        rows=self._read_data('scheduled_content')
        item=next((x for x in rows if x['id']==schedule_id),None)
        if not item or item.get('queued') or item.get('sent'):return False
        content=self.get_stage_content_by_id(item['content_id'])
        if not content:
            item['sent']=True;self._write_data('scheduled_content',rows);return False
        for uid, in self._execute('SELECT p.telegram_id FROM student_profiles p JOIN users u ON u.telegram_id=p.telegram_id WHERE p.stage=? AND u.is_banned=0',(content[1],)).fetchall():
            if not self.notifications_enabled(uid) or self.is_delivery_blocked(uid):continue
            previous=item.get('deliveries',{}).get(str(uid),{}).get('state')
            if previous in ('sent','failed_permanent'):continue
            jid=f'scheduled:{schedule_id}:{uid}'
            self.enqueue_job(jid,'scheduled',{'user_id':uid,'content_id':content[0],'schedule_id':schedule_id})
            if previous in ('sending','uncertain'):
                self.finish_job(jid,'uncertain','Interrupted legacy scheduled send')
        item['queued']=True
        self._write_data('scheduled_content',rows)
        return True

    def queue_support_reply(self,ticket_id,actor,reply):
        tickets=self._read_data('support_tickets')
        ticket=next((x for x in tickets if x['id']==ticket_id),None)
        if not ticket or ticket.get('status','open')!='open':raise ValueError('Ticket is already closed or being answered')
        if actor!=DEVELOPER_ID and self.get_support_admin(ticket['stage'])!=actor:raise PermissionError('Wrong support stage')
        if not self.is_admin(actor) or self.is_banned(actor) or not 1<=len(reply.strip())<=2000:raise ValueError('Invalid reply')
        ticket.update(status='replying',reply=reply.strip(),reply_by=actor)
        self._write_data('support_tickets',tickets)
        self.enqueue_job(f'support-reply:{ticket_id}','support_reply',{'ticket_id':ticket_id,'user_id':ticket['user_id'],'actor':actor,'reply':reply.strip()})
        return True

    def enqueue_job(self,job_id,kind,payload):
        self._execute("INSERT INTO delivery_jobs(id,kind,payload,updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",(job_id,kind,json.dumps(payload,ensure_ascii=False),time.time()))

    def recover_jobs(self):
        self._execute("UPDATE delivery_jobs SET state='uncertain',error='Interrupted during send',updated_at=? WHERE state='sending'",(time.time(),))

    def claim_job(self):
        row = self._execute("SELECT id,kind,payload,attempts FROM delivery_jobs WHERE state='pending' AND next_attempt<=? ORDER BY updated_at LIMIT 1",(time.time(),)).fetchone()
        if not row: return None
        updated = self._execute("UPDATE delivery_jobs SET state='sending',attempts=attempts+1,updated_at=? WHERE id=? AND state='pending'",(time.time(),row[0]))
        if updated.rowcount != 1: return None
        return {'id':row[0],'kind':row[1],'payload':json.loads(row[2]),'attempts':row[3]+1}

    def finish_job(self,job_id,state,error=None,retry_after=0):
        self._execute('UPDATE delivery_jobs SET state=?,error=?,next_attempt=?,updated_at=? WHERE id=?',(state,error,time.time()+retry_after,time.time(),job_id))
        row=self._execute('SELECT kind,payload FROM delivery_jobs WHERE id=?',(job_id,)).fetchone()
        if row and row[0]=='scheduled':
            payload=json.loads(row[1]);sid=payload['schedule_id']
            rows=self._read_data('scheduled_content')
            for item in rows:
                if item['id']!=sid:continue
                item.setdefault('deliveries',{})[str(payload['user_id'])]={'state':state,'at':time.time(),'error':error}
                prefix=f'scheduled:{sid}:%'
                counts=dict(self._execute('SELECT state,COUNT(*) FROM delivery_jobs WHERE id LIKE ? GROUP BY state',(prefix,)).fetchall())
                item['sent']=bool(counts and set(counts)=={'sent'})
                item['status']='sent' if item['sent'] else 'needs_review' if counts.get('failed') or counts.get('uncertain') else 'queued'
                self._write_data('scheduled_content',rows)
                break

    def unresolved_jobs(self):
        return [tuple(row) for row in self._execute("SELECT id,kind,state,error FROM delivery_jobs WHERE state IN ('uncertain','failed') ORDER BY updated_at DESC LIMIT 30").fetchall()]

    def retry_job(self,job_id,actor):
        if actor != DEVELOPER_ID: raise PermissionError('Developer only')
        result = self._execute("UPDATE delivery_jobs SET state='pending',attempts=0,next_attempt=0,updated_at=? WHERE id=? AND state IN ('uncertain','failed')",(time.time(),job_id))
        self.log_action(actor,'delivery_retry',{'job':job_id})
        return bool(result.rowcount)

    def close_support_ticket(self,ticket_id,actor,reply=None):
        tickets = self._read_data('support_tickets')
        for ticket in tickets:
            if ticket['id'] != ticket_id: continue
            if actor != DEVELOPER_ID and self.get_support_admin(ticket['stage']) != actor:
                raise PermissionError('Wrong support stage')
            if ticket.get('status','open') not in ('open','replying'): return False
            if ticket.get('status')=='replying' and (ticket.get('reply_by')!=actor or reply is None):raise PermissionError('Reply belongs to another operator')
            ticket.update(status='closed',closed_by=actor,closed_at=datetime.now(IRAQ_TZ).isoformat(),reply=reply)
            self._write_data('support_tickets',tickets)
            self.log_action(actor,'support_closed',{'ticket':ticket_id})
            return True
        return False

    def can_manage_stage(self,actor,stage):
        if self.is_banned(actor): return False
        if actor == DEVELOPER_ID: return True
        if not self.is_admin(actor): return False
        scopes = self._read_data('settings').get('admin_stages',{}).get(str(actor))
        if scopes is None:
            profile = self.get_user_stage(actor)
            return bool(profile and profile[0] == stage)
        return stage in scopes

    def set_admin_stages(self,actor,admin_id,stages):
        if actor != DEVELOPER_ID: raise PermissionError('Developer only')
        if not self.is_admin(admin_id) or not stages or any(type(x) is not int or x not in (1,2,3,4) for x in stages): raise ValueError('Invalid admin stages')
        settings = self._read_data('settings')
        settings.setdefault('admin_stages',{})[str(admin_id)] = sorted(set(stages))
        self._write_data('settings',settings)
        self.log_action(actor,'admin_stages',{'admin':admin_id,'stages':stages})

    def mark_daily_physics(self,user_id):
        self._execute('INSERT INTO daily_physics(telegram_id,sent_date) VALUES (?,?) ON CONFLICT(telegram_id) DO UPDATE SET sent_date=excluded.sent_date',(user_id,datetime.now(IRAQ_TZ).date().isoformat()))

    def get_daily_physics_recipients(self):
        today = datetime.now(IRAQ_TZ).date().isoformat()
        return [row[0] for row in self._execute("SELECT p.telegram_id FROM student_profiles p JOIN users u ON u.telegram_id=p.telegram_id LEFT JOIN daily_physics d ON d.telegram_id=p.telegram_id WHERE u.is_banned=0 AND (d.sent_date IS NULL OR d.sent_date<>?)",(today,)).fetchall() if self.notifications_enabled(row[0]) and not self.is_delivery_blocked(row[0])]

    def worker_lease_alive(self):
        if not self._worker_claimed: return False
        if self._backend == 'sqlite': return self._worker_file is not None
        if self._lease_connection is None or self._lease_connection.closed: return False
        try:
            return self._lease_connection.execute('SELECT 1').fetchone()[0] == 1
        except Exception: return False

    def schedule_delivery(self, schedule_id, user_id, state, error=None):
        rows = self._read_data('scheduled_content')
        for row in rows:
            if row['id'] == schedule_id:
                row.setdefault('deliveries', {})[str(user_id)] = {'state': state, 'at': time.time(), 'error': error}
                self._write_data('scheduled_content', rows)
                return

_SCOPES = {
    'add_user':('users',),'update_user_activity':('users',),'ban_user':('users',),'unban_user':('users',),
    'set_user_stage':('users','users_stages'),'add_admin':('admins',),'remove_admin':('admins','settings'),
    'set_support_admin':('settings',),'set_admin_stages':('settings','audit_log'),
    'add_required_channel':('required_channels',),'remove_required_channel':('required_channels',),
    'add_search_channel':('search_channels',),'remove_search_channel':('search_channels',),
    'add_support_ticket':('support_tickets','delivery_jobs'),'delete_support_ticket':('support_tickets',),
    'close_support_ticket':('support_tickets','audit_log'),
    'add_stage_content':('stage_content',),'update_content_description':('stage_content',),
    'delete_stage_content':('stage_content','comments','favorites','progress','scheduled_content','comments_meta'),
    'add_comment':('comments',),'delete_comment':('comments','comments_meta'),
    'set_ai_enabled':('settings',),'set_physics_info_channel':('physics_channel',),'remove_physics_info_channel':('physics_channel',),
    'add_user_physics_info_request':('physics_requests',),
    'reserve_physics_fact':('physics_fact_state',),'complete_physics_fact':('physics_fact_state',),
    'cancel_physics_fact':('physics_fact_state',),'mark_daily_physics':('daily_physics',),
    'toggle_favorite':('favorites',),'mark_content_viewed':('progress',),
    'delete_quiz':('quizzes','quiz_attempts','audit_log'),'add_quiz':('quizzes',),'get_random_quiz':('quizzes','settings'),'record_quiz_attempt':('quizzes','quiz_attempts','settings'),
    'set_delivery_blocked':('notification_preferences',),'toggle_notifications':('notification_preferences',),'schedule_content':('scheduled_content',),
    'mark_schedule_sent':('scheduled_content',),'schedule_delivery':('scheduled_content',),
    'log_action':('audit_log',),'add_chapter':('settings','audit_log'),'remove_chapter':('settings','audit_log'),
    'complete_quiz_draft':('workflow_state','quizzes','audit_log'),
    'finish_upload':('workflow_state','stage_content','delivery_jobs'),
    'set_workflow':('workflow_state',),'get_workflow':('workflow_state',),
    'queue_schedule':('delivery_jobs','scheduled_content'),'queue_support_reply':('support_tickets','delivery_jobs'),'enqueue_job':('delivery_jobs',),'recover_jobs':('delivery_jobs',),'claim_job':('delivery_jobs',),
    'finish_job':('delivery_jobs','scheduled_content'),'retry_job':('delivery_jobs','audit_log'),
    'add_ai_daily_usage':('ai_budget',),'reserve_ai':('ai_budget',),'settle_ai':('ai_budget',),
    'insert_default_data':('subjects','admins'),
}

def _atomic_method(method):
    name=method.__name__
    readonly=(name.startswith(('get_','is_','count_','can_','workflow_users','chapter_label','lab_','notifications_enabled','search_content')) or name in ('ping','_default_value','_get_user_by_id','_record_key','_read_data','unresolved_jobs')) and name not in ('get_random_quiz','get_workflow')
    @functools.wraps(method)
    def call(self,*args,**kwargs):
        if self._transaction_depth:return method(self,*args,**kwargs)
        lock=contextlib.nullcontext() if self._pool else self._lock
        with lock:
            for attempt in range(3):
                context=self._pool.connection() if self._pool else contextlib.nullcontext(self._initial_connection)
                with context as connection:
                    self._local.connection=connection
                    try:
                        connection.commit()
                        if not readonly:
                            if self._backend=='postgresql':
                                scopes=_SCOPES.get(name,tuple(self.data_keys))
                                if name in ('_safe_write','_write_data','_mutate_data','_put_row'):scopes=(args[0],)
                                if name=='rate_limit':scopes=('rate:'+str(args[0])+':'+str(args[1]),)
                                if name=='_next_id':scopes=('seq:'+str(args[0]),)
                                ids=sorted({int.from_bytes(hashlib.sha256(('bot4stage:'+scope).encode()).digest()[:8],'big',signed=True) for scope in scopes})
                                for ident in ids:self._execute('SELECT pg_advisory_xact_lock(?)',(ident,))
                            else:self._execute('BEGIN IMMEDIATE')
                        self._transaction_depth=1
                        value=method(self,*args,**kwargs)
                        connection.commit()
                        return value
                    except Exception as error:
                        connection.rollback()
                        if getattr(error,'sqlstate',None) in ('40P01','40001') and attempt<2:continue
                        raise
                    finally:
                        self._transaction_depth=0
                        del self._local.connection
    return call
for _name,_method in list(vars(Database).items()):
    if callable(_method) and _name not in {'__init__','_connect_database','_execute','_create_relational_schema','_upgrade_chapter_schema','_migrate_legacy_data','_migration_done','_migration_mark','_repair_projections','_sync_projection','_migrate_rows','create_backup','restore_postgres_backup','confirm_backup_delivery','_prune_backups','worker_lease_alive','close'}:
        setattr(Database,_name,_atomic_method(_method))

class LazyDatabase:
    def __init__(self):
        object.__setattr__(self,'_instance',None)
        object.__setattr__(self,'_initialize_lock',threading.Lock())
        object.__setattr__(self,'_stop_event',threading.Event())
    def initialize(self):
        if self._instance is None:
            with self._initialize_lock:
                if self._instance is None: object.__setattr__(self,'_instance',Database())
        return self._instance
    @property
    def initialized(self):return self._instance is not None
    def initialize_for_worker(self,timeout=300):
        if self._instance is None:
            with self._initialize_lock:
                if self._instance is None:
                    instance=Database(wait_for_worker=True,startup_timeout=timeout,stop_event=self._stop_event)
                    if self._stop_event.is_set():
                        instance.close();raise RuntimeError('Startup was stopped')
                    object.__setattr__(self,'_instance',instance)
        return self._instance
    def close(self):
        self._stop_event.set()
        if self._instance is not None:self._instance.close()
    def __getattr__(self,name): return getattr(self.initialize(),name)
    def __setattr__(self,name,value):
        if name.startswith('_instance'):object.__setattr__(self,name,value)
        else:setattr(self.initialize(),name,value)
    def __delattr__(self,name):delattr(self.initialize(),name)

db = LazyDatabase()
