"""Authenticated Telegram Mini App for the student features."""
import asyncio
import hashlib, hmac, json, logging, time
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import parse_qsl
from aiohttp import ClientSession, web
from config import BOT_TOKEN, DEVELOPER_ID, MINI_APP_PORT, client
from database import db
WEB_ROOT = Path(__file__).with_name('web')
MAX_INIT_DATA_AGE = 3600
LIMITS = {'ai': (5, 60), 'search': (20, 60), 'quiz': (30, 60), 'comments': (10, 60), 'support': (5, 300), 'favorites': (30, 60), 'send': (10, 60), 'content': (60, 60)}
_rate_windows = defaultdict(deque)

def _bad(message='الطلب غير صالح.', status=400):
    errors = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict, 429: web.HTTPTooManyRequests, 503: web.HTTPServiceUnavailable, 502: web.HTTPBadGateway}
    raise errors.get(status, web.HTTPBadRequest)(reason=message, text=json.dumps({'error': message}), content_type='application/json')

def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        _bad('معرّف غير صالح.')

async def _body(r):
    try:
        d = await r.json()
    except (ValueError, json.JSONDecodeError):
        _bad('بيانات الطلب غير صالحة.')
    if not isinstance(d, dict):
        _bad('بيانات الطلب غير صالحة.')
    return d

async def _limit(uid, bucket):
    count, period = LIMITS[bucket]
    if not await asyncio.to_thread(db.rate_limit, uid, bucket, count, period):
        _bad('الطلبات كثيرة، حاول بعد قليل.', 429)

async def ident(r):
    raw = r.headers.get('X-Telegram-Init-Data', '')
    if not BOT_TOKEN or not raw or len(raw) > 8192:
        _bad('افتحه من تيليغرام.', 401)
    pairs = parse_qsl(raw, keep_blank_values=True)
    if len(pairs) != len({k for k, _ in pairs}):
        _bad('بيانات تيليغرام غير صالحة.', 401)
    v = dict(pairs)
    got = v.pop('hash', None)
    try:
        auth_date = int(v.get('auth_date', ''))
    except (TypeError, ValueError):
        _bad('بيانات تيليغرام غير صالحة.', 401)
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > MAX_INIT_DATA_AGE:
        _bad('انتهت صلاحية جلسة تيليغرام، أعد فتح التطبيق.', 401)
    key = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    check = '\n'.join((f'{k}={v[k]}' for k in sorted(v)))
    if not got or not hmac.compare_digest(got, hmac.new(key, check.encode(), hashlib.sha256).hexdigest()):
        _bad('بيانات تيليغرام غير صالحة.', 401)
    try:
        u = json.loads(v.get('user', '{}'))
        uid = int(u['id'])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _bad('بيانات المستخدم غير صالحة.', 401)
    if not isinstance(u, dict) or uid <= 0 or await asyncio.to_thread(db.is_banned, uid):
        _bad('لا تملك صلاحية الوصول.', 403)
    if not await asyncio.to_thread(db.rate_limit, uid, 'web', 120, 60):
        _bad('طلبات كثيرة.', 429)
    from utils import check_subscription
    from utils import SubscriptionUnavailable
    try:
        subscribed = await check_subscription(uid)
    except SubscriptionUnavailable:
        _bad('تعذر التحقق من الاشتراك مؤقتاً، حاول بعد قليل.',503)
    if not subscribed:
        _bad('يجب الاشتراك في القنوات المطلوبة أولاً.', 403)
    await asyncio.to_thread(db.update_user_activity, uid)
    return (uid, u)

async def own(uid, cid):
    x = await asyncio.to_thread(db.get_stage_content_by_id, cid)
    s = await asyncio.to_thread(db.get_user_stage, uid)
    if not x or not s or x[1] != s[0]:
        _bad('المحتوى غير متاح.', 404)
    return x

async def pack(x):
    i, st, k, ch, typ, f, n, t, d, _, no = x
    sub = await asyncio.to_thread(db.get_stage_subject, st, k)
    return {'id': i, 'chapter': ch, 'title': d or n or f'محتوى #{no}', 'text': t or '', 'has_file': bool(f) and typ != 'text', 'subject': sub[0] if sub else k}

async def index(r):
    """Version assets by content so Telegram WebViews receive each deployment."""
    html = (WEB_ROOT / 'index.html').read_text(encoding='utf-8')
    for asset in ('js/app.js', 'css/style.css'):
        version = hashlib.sha256((WEB_ROOT / asset).read_bytes()).hexdigest()[:16]
        html = html.replace(f'/static/{asset}', f'/static/{asset}?v={version}')
    return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-cache'})

def worker_ready():
    phase = getattr(client, 'runtime_phase', 'running')
    tasks = getattr(client, 'background_tasks', [])
    return (phase == 'running' and client.is_connected() and bool(tasks)
            and all(not task.done() for task in tasks))

async def _health_state():
    phase=getattr(client,'runtime_phase','running')
    if not db.initialized:
        transitional=phase in ('waiting_worker','starting') and time.monotonic()<getattr(client,'startup_deadline',0)
        return {'ok':transitional,'database':False,'worker_ready':False,'phase':phase}
    try:
        database = bool(await asyncio.wait_for(asyncio.to_thread(db.ping), 5))
        lease = await asyncio.to_thread(db.worker_lease_alive) if getattr(client,'runtime_phase','running') == 'running' else True
    except Exception:
        database = False
        lease = False
    phase = getattr(client, 'runtime_phase', 'running')
    transitional = phase in ('waiting_worker', 'starting') and time.monotonic() < getattr(client, 'startup_deadline', 0)
    ready = bool(database and lease and worker_ready())
    return {'ok': bool(database and (ready or transitional)), 'worker_ready': ready,
            'phase': phase, 'database': database}

async def health(r):
    """HTTP health accepts a bounded standby period for Render handover."""
    state = await _health_state()
    return web.json_response(state, status=200 if state['ok'] else 503)

async def ready(r):
    """Separate endpoint for monitoring actual Telegram worker readiness."""
    state = await _health_state()
    return web.json_response(state, status=200 if state['worker_ready'] else 503)

async def me(r):
    uid, u = await ident(r)
    s = await asyncio.to_thread(db.get_user_stage, uid)
    return web.json_response({'registered': bool(s), 'stage': s[0] if s else None, 'full_name': s[1] if s else u.get('first_name', ''), 'notifications': await asyncio.to_thread(db.notifications_enabled, uid)})

async def register(r):
    uid, u = await ident(r)
    d = await _body(r)
    n = str(d.get('full_name', '')).strip()
    st = d.get('stage')
    if await asyncio.to_thread(db.get_user_stage, uid):
        _bad('المرحلة مسجلة مسبقاً ولا يمكن تغييرها من الموقع.', 409)
    if len(n) > 120 or len(n.split()) < 2 or (not isinstance(st, int)) or isinstance(st, bool) or (st not in (1, 2, 3, 4)):
        _bad('تحقق من الاسم والمرحلة.')
    await asyncio.to_thread(db.add_user, uid, u.get('username'), u.get('first_name', ''), u.get('last_name', ''))
    await asyncio.to_thread(db.set_user_stage, uid, n, st)
    return web.json_response({'ok': True})

async def subjects(r):
    uid, _ = await ident(r)
    s = await asyncio.to_thread(db.get_user_stage, uid)
    c = r.match_info['c']
    names = {'subjects': 'المواد الدراسية', 'explanations': 'الشروحات', 'lab': 'المختبر', 'monthly_exams': 'الامتحانات الشهرية', 'final_exams': 'الامتحانات النهائية', 'research': 'قسم الرابعة'}
    if not s or c not in names or (c == 'research' and s[0] != 4):
        _bad('القسم غير متاح.', 404)
    return web.json_response({'title': names[c], 'items': [{'name': n, 'key': k} for n, k in await asyncio.to_thread(db.get_stage_subjects_by_category, s[0], c)]})

async def chapters(r):
    uid, _ = await ident(r)
    profile = await asyncio.to_thread(db.get_user_stage, uid)
    key = r.match_info['k']
    subject = await asyncio.to_thread(db.get_stage_subject, profile[0], key) if profile else None
    if not subject or subject[3] == 'archived_lab':
        _bad('المادة غير متاحة.', 404)
    items = await asyncio.to_thread(db.get_chapters, profile[0], key)
    return web.json_response({'name': subject[0], 'chapters': [x['id'] for x in items], 'items': items, 'lab': subject[3] == 'lab'})

async def content_list(r):
    uid, _ = await ident(r)
    await _limit(uid, 'content')
    s = await asyncio.to_thread(db.get_user_stage, uid)
    k = r.query.get('subject', '')
    c = _integer(r.query.get('chapter'))
    sub = await asyncio.to_thread(db.get_stage_subject, s[0], k) if s else None
    if not sub or not await asyncio.to_thread(db.is_valid_chapter, s[0], k, c):
        _bad('طلب محتوى غير صالح.')
    ic = {'video': '🎥', 'document': '📄', 'photo': '🖼️', 'text': '📝', 'audio': '🎵', 'voice': '🎤'}
    label = await asyncio.to_thread(db.chapter_label, s[0], k, c)
    return web.json_response({'subject': sub[0], 'label': label, 'items': [{'id': x[0], 'title': x[2] or f'محتوى #{x[4]}', 'icon': ic.get(x[1], '📎')} for x in await asyncio.to_thread(db.get_stage_content, s[0], k, c)]})

async def content(r):
    uid, _ = await ident(r)
    await _limit(uid, 'content')
    x = await own(uid, _integer(r.match_info['i']))
    await asyncio.to_thread(db.mark_content_viewed, uid, x[0])
    d = await pack(x)
    d['favorite'] = await asyncio.to_thread(db.is_favorite, uid, x[0])
    return web.json_response(d)

async def send(r):
    uid, _ = await ident(r)
    await _limit(uid, 'send')
    x = await own(uid, _integer(r.match_info['i']))
    from utils import ContentSender
    if not await ContentSender.send_stage_single_content(uid, x, await asyncio.to_thread(db.is_admin, uid)):
        _bad('تعذر إرسال المحتوى إلى المحادثة.', 502)
    return web.json_response({'ok': True})

async def favorites(r):
    uid, _ = await ident(r)
    s = await asyncio.to_thread(db.get_user_stage, uid)
    return web.json_response({'items': [await pack(x) for x in await asyncio.to_thread(db.get_user_favorites, uid) if s and x[1] == s[0]]})

async def favorite(r):
    uid, _ = await ident(r)
    await _limit(uid, 'favorites')
    i = _integer(r.match_info['i'])
    await own(uid, i)
    return web.json_response({'enabled': await asyncio.to_thread(db.toggle_favorite, uid, i)})

async def progress(r):
    uid, _ = await ident(r)
    return web.json_response(await asyncio.to_thread(db.get_user_progress, uid))

async def notifications(r):
    uid, _ = await ident(r)
    return web.json_response({'enabled': await asyncio.to_thread(db.toggle_notifications, uid)})

async def search(r):
    uid, _ = await ident(r)
    await _limit(uid, 'search')
    profile = await asyncio.to_thread(db.get_user_stage, uid)
    query = ' '.join(r.query.get('q', '').split())
    page = max(0, _integer(r.query.get('page', 0)))
    if not profile or not 2 <= len(query) <= 120:
        _bad('اكتب من حرفين إلى 120 حرفاً للبحث.')
    matches = await asyncio.to_thread(db.search_content, query, profile[0], limit=6, offset=page * 5)
    return web.json_response({'page': page, 'has_more': len(matches) > 5, 'items': [{'id': c['id'], 'title': c.get('description') or c.get('file_name') or c['content_type'], 'subject': name} for c, name in matches[:5]]})

async def quiz(r):
    uid, _ = await ident(r)
    await _limit(uid, 'quiz')
    s = await asyncio.to_thread(db.get_user_stage, uid)
    q = await asyncio.to_thread(db.get_random_quiz, s[0], uid) if s else None
    from quiz_tools import public_quiz
    return web.json_response({'quiz':public_quiz(q) if q else None})

async def answer(r):
    uid, _ = await ident(r)
    await _limit(uid, 'quiz')
    s = await asyncio.to_thread(db.get_user_stage, uid)
    q = await asyncio.to_thread(db.get_quiz, _integer(r.match_info['i']))
    d = await _body(r)
    answer_index = d.get('answer')
    blank=bool(q and q.get('question_type')=='fill_blank')
    valid=(isinstance(answer_index,str) and 1<=len(answer_index.strip())<=100) if blank else (type(answer_index) is int and answer_index in range(len(q.get('options',[])) if q else 0))
    if not s or not q or not q['enabled'] or q.get('stage') != s[0] or not valid:
        _bad('إجابة الاختبار غير صالحة.')
    result = await asyncio.to_thread(db.record_quiz_attempt, uid, q['id'], answer_index)
    if result is None:
        _bad('ابدأ محاولة الاختبار أولاً.', 409)
    from quiz_tools import correct_label
    return web.json_response({'correct':bool(result),'correct_answer':correct_label(q)})

async def comments(r):
    uid, _ = await ident(r)
    i = _integer(r.match_info['i'])
    await own(uid, i)
    if r.method == 'POST':
        await _limit(uid, 'comments')
        t = str((await _body(r)).get('text', '')).strip()
        if not t or len(t) > 1000:
            _bad('طول التعليق غير صالح.')
        if not await asyncio.to_thread(db.add_comment, uid, i, t, (await asyncio.to_thread(db.get_user_stage, uid))[0]):
            _bad('انتظر قليلاً قبل إضافة تعليق آخر.', 429)
        return web.json_response({'ok': True})
    offset = max(0, _integer(r.query.get('offset', 0)))
    rows = await asyncio.to_thread(db.get_comments_page,i,offset,21)
    return web.json_response({'has_more': len(rows) > 20, 'items': [{'id': x[0], 'name': x[4] or x[5] or 'طالب', 'text': x[2], 'can_delete': x[1] == uid or await asyncio.to_thread(db.can_manage_stage,uid,(await asyncio.to_thread(db.get_user_stage,uid))[0])} for x in rows[:20]]})

async def delete_comment(r):
    uid, _ = await ident(r)
    await _limit(uid, 'comments')
    return web.json_response({'ok': await asyncio.to_thread(db.delete_comment, _integer(r.match_info['i']), uid)})

async def support(r):
    uid, _ = await ident(r)
    await _limit(uid, 'support')
    s = await asyncio.to_thread(db.get_user_stage, uid)
    t = str((await _body(r)).get('message', '')).strip()
    if not s or not t or len(t) > 2000:
        _bad('رسالة الدعم غير صالحة.')
    ticket_id = await asyncio.to_thread(db.add_support_ticket, uid, t, s[0])
    return web.json_response({'id': ticket_id})

async def ai(r):
    uid, _ = await ident(r)
    await _limit(uid, 'ai')
    s = await asyncio.to_thread(db.get_user_stage, uid)
    q = str((await _body(r)).get('question', '')).strip()
    if not s or not 3 <= len(q) <= 1500:
        _bad('السؤال غير صالح.')
    if not await asyncio.to_thread(db.get_ai_enabled):
        return web.json_response({'answer': '⚠️ الذكاء الاصطناعي معطل حالياً.'})
    from ai_handler import explain_question
    return web.json_response({'answer': await explain_question(uid, q, s[0])})

async def set_menu_button(token, url):
    from aiohttp import ClientTimeout
    async with ClientSession(timeout=ClientTimeout(total=15)) as s:
        async with s.post(f'https://api.telegram.org/bot{token}/setChatMenuButton', json={'menu_button': {'type': 'web_app', 'text': 'Open', 'web_app': {'url': url}}}) as r:
            d = await r.json()
    if not d.get('ok'):
        raise RuntimeError(d.get('description', 'تعذر إعداد زر Open'))
_runners = []

@web.middleware
async def api_errors(request, handler):
    try:
        if request.path.startswith('/api/') and getattr(client, 'runtime_phase', 'running') != 'running':
            return web.json_response(
                {'error': 'البوت ينتقل إلى النسخة الجديدة، أعد المحاولة بعد قليل.'},
                status=503, headers={'Retry-After': '5', 'Cache-Control': 'no-store'})
        response = await handler(request)
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response
    except web.HTTPException as error:
        if request.path.startswith('/api/'):
            message = error.reason if error.reason else 'تعذر تنفيذ الطلب.'
            return web.json_response({'error': message}, status=error.status)
        raise
    except Exception:
        logging.exception('Unhandled Mini App request error: %s', request.path)
        if request.path.startswith('/api/'):
            return web.json_response({'error': 'تعذر تنفيذ الطلب، حاول لاحقاً.'}, status=500)
        raise

async def start_webapp():
    app = web.Application(middlewares=[api_errors], client_max_size=64 * 1024)
    app.router.add_static('/static/', WEB_ROOT, show_index=False)
    app.add_routes([web.get('/', index), web.get('/health', health), web.get('/ready', ready), web.get('/api/me', me), web.post('/api/register', register), web.get('/api/subjects/{c}', subjects), web.get('/api/subjects/{k}/chapters', chapters), web.get('/api/content', content_list), web.get('/api/content/{i}', content), web.post('/api/content/{i}/send', send), web.get('/api/favorites', favorites), web.post('/api/favorites/{i}', favorite), web.get('/api/progress', progress), web.post('/api/notifications', notifications), web.get('/api/search', search), web.get('/api/quiz', quiz), web.post('/api/quiz/{i}/answer', answer), web.get('/api/content/{i}/comments', comments), web.post('/api/content/{i}/comments', comments), web.delete('/api/comments/{i}', delete_comment), web.post('/api/support', support), web.post('/api/ai', ai)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', MINI_APP_PORT).start()
    _runners.append(runner)
    return runner

async def stop_webapp():
    for runner in _runners:
        await runner.cleanup()
    _runners.clear()
