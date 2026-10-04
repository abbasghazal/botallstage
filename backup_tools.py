"""PostgreSQL tools with explicit connection settings and private credentials."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

@contextlib.contextmanager
def postgres_environment(dsn):
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(dsn)
    if not params.get('host') or not params.get('dbname') or not params.get('user'):
        raise ValueError('Backup connection requires an explicit host, database and user')
    # Remove inherited libpq connection settings: the application DSN is authoritative.
    env = {k:v for k,v in os.environ.items() if not k.startswith('PG')}
    names = {'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER','sslmode':'PGSSLMODE','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','sslrootcert':'PGSSLROOTCERT','sslcrl':'PGSSLCRL','options':'PGOPTIONS','target_session_attrs':'PGTARGETSESSIONATTRS','channel_binding':'PGCHANNELBINDING','application_name':'PGAPPNAME'}
    env.update({names[k]:str(v) for k,v in params.items() if k in names})
    env['LC_ALL'] = 'C'
    env['PGCONNECT_TIMEOUT'] = str(params.get('connect_timeout',15))
    with tempfile.TemporaryDirectory(prefix='bot4stage-pg-') as directory:
        password = params.get('password')
        if password is not None:
            def escape(value): return str(value).replace('\\','\\\\').replace(':','\\:')
            path = Path(directory)/'passfile'
            # Wildcards support libpq multi-host DSNs; the file is private and short-lived.
            path.write_text('*:*:'+escape(params['dbname'])+':'+escape(params['user'])+':'+escape(password)+'\n')
            path.chmod(0o600)
            env['PGPASSFILE'] = str(path)
        elif params.get('passfile'):
            env['PGPASSFILE'] = params['passfile']
        yield env, params['dbname']

class BackupToolError(RuntimeError):
    """Only fixed, credential-free diagnostics may leave this module."""
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(f'[{code}] {detail}')


def classify_tool_failure(stderr):
    text = (stderr or '').lower()
    rules = [
        ('version_mismatch', ('server version mismatch', 'aborting because of server version'), 'إصدار pg_dump أقدم من خادم PostgreSQL. حدّث أدوات النسخ ثم أعد النشر.'),
        ('authentication', ('password authentication failed', 'no password supplied', 'authentication failed'), 'رفضت قاعدة البيانات بيانات الدخول. راجع DATABASE_URL وكلمة المرور.'),
        ('access_rule', ('no pg_hba.conf entry',), 'خادم PostgreSQL لا يسمح بهذا الاتصال. راجع قواعد الوصول ومتطلبات SSL.'),
        ('ssl', ('ssl error', 'ssl connection', 'certificate verify failed', 'root certificate', 'does not support ssl'), 'فشل اتصال SSL أو التحقق من الشهادة. راجع sslmode والشهادات.'),
        ('dns', ('could not translate host name', 'name or service not known', 'temporary failure in name resolution'), 'تعذر حل اسم مضيف PostgreSQL. راجع عنوان الاتصال وDNS.'),
        ('connection', ('connection refused', 'connection timed out', 'timeout expired', 'network is unreachable', 'server closed the connection'), 'تعذر الاتصال بخادم PostgreSQL أو انقطع الاتصال. راجع الشبكة والمضيف والمنفذ.'),
        ('permission', ('permission denied', 'must be superuser', 'insufficient privilege'), 'الصلاحيات غير كافية لقراءة القاعدة أو كتابة ملف النسخ.'),
        ('disk_full', ('no space left on device', 'disk quota exceeded'), 'المساحة المتاحة لملف النسخ غير كافية.'),
        ('missing_database', ('does not exist',), 'أحد موارد قاعدة البيانات المطلوبة غير موجود. راجع اسم القاعدة وإعداداتها.'),
    ]
    for code, needles, detail in rules:
        if any(needle in text for needle in needles): return code, detail
    return 'unknown', 'سبب غير مصنّف؛ راجع الاتصال والصلاحيات وإعدادات الخادم. لم تُطبع رسالة الأداة لحماية بيانات الاتصال.'


def run_tool(command, env, timeout=180):
    try:
        return subprocess.run(command, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        code, detail = classify_tool_failure(error.stderr)
        raise BackupToolError(code, f'{Path(command[0]).name} (exit {int(error.returncode)}): {detail}') from None
    except subprocess.TimeoutExpired:
        raise BackupToolError('timeout', 'انتهت مهلة أداة PostgreSQL. راجع اتصال الخادم وحجم القاعدة.') from None
    except FileNotFoundError:
        raise BackupToolError('missing_tool', 'أداة PostgreSQL غير مثبتة. أعد بناء خدمة Docker باستخدام Dockerfile المرفق.') from None
    except OSError:
        raise BackupToolError('tool_execution', 'تعذر تشغيل أداة PostgreSQL. راجع تثبيتها وصلاحيات التشغيل.') from None


def check_backup_versions(server_version_num, env):
    versions = {}
    for tool in ('pg_dump', 'pg_restore'):
        output = run_tool([tool, '--version'], env, timeout=15).stdout
        match = re.search(r'PostgreSQL\)\s+(\d+)(?:\.\d+)*', output or '')
        if not match:
            raise BackupToolError('tool_version', 'تعذر قراءة إصدار أدوات PostgreSQL.')
        versions[tool] = int(match.group(1))
    server = int(server_version_num) // 10000
    if server < 10 or versions['pg_dump'] < server:
        raise BackupToolError('version_mismatch', f"PostgreSQL server={server}, pg_dump={versions['pg_dump']}. حدّث أدوات النسخ إلى إصدار الخادم أو أحدث وأعد النشر.")
    if versions['pg_restore'] < versions['pg_dump']:
        raise BackupToolError('restore_version', f"pg_dump={versions['pg_dump']}, pg_restore={versions['pg_restore']}. ثبّت أدوات النسخ والاستعادة المتوافقة.")
    return versions

def digest(path):
    value = hashlib.sha256()
    with open(path,'rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''): value.update(chunk)
    return value.hexdigest()

def write_manifest(path):
    metadata = {'file':Path(path).name,'bytes':Path(path).stat().st_size,'sha256':digest(path),'validated':True,'remote_confirmed':False}
    Path(str(path)+'.json').write_text(json.dumps(metadata,indent=2))
    return metadata

def validate_manifest(path):
    metadata = json.loads(Path(str(path)+'.json').read_text())
    if metadata.get('sha256') != digest(path) or metadata.get('bytes') != Path(path).stat().st_size:
        raise ValueError('Backup checksum or size mismatch')
    return metadata
