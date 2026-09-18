"""Authenticated Telegram Mini App for the student features."""
import hashlib, hmac, json, logging, time
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import parse_qsl
from aiohttp import ClientSession, web
from config import BOT_TOKEN, DEVELOPER_ID, MINI_APP_PORT, client
from database import db

WEB_ROOT = Path(__file__).with_name('web')

MAX_INIT_DATA_AGE = 3600
LIMITS = {'ai': (5, 60), 'search': (20, 60), 'quiz': (30, 60), 'comments': (10, 60), 'support': (5, 300), 'favorites': (30, 60), 'content': (60, 60)}
_rate_windows = defaultdict(deque)

def _bad(message='الطلب غير صالح.', status=400):
 errors={400:web.HTTPBadRequest,401:web.HTTPUnauthorized,403:web.HTTPForbidden,404:web.HTTPNotFound,409:web.HTTPConflict,429:web.HTTPTooManyRequests,502:web.HTTPBadGateway}
 raise errors.get(status,web.HTTPBadRequest)(reason=message,text=json.dumps({'error':message}),content_type='application/json')

def _integer(value):
 try:return int(value)
 except (TypeError,ValueError):_bad('معرّف غير صالح.')

async def _body(r):
 try:d=await r.json()
 except (ValueError,json.JSONDecodeError):_bad('بيانات الطلب غير صالحة.')
 if not isinstance(d,dict):_bad('بيانات الطلب غير صالحة.')
 return d

def _limit(uid, bucket):
 count,period=LIMITS[bucket]; now=time.monotonic(); window=_rate_windows[(uid,bucket)]
 while window and now-window[0]>=period:window.popleft()
 if len(window)>=count:_bad('الطلبات كثيرة، حاول بعد قليل.',429)
 window.append(now)

def ident(r):
 raw=r.headers.get('X-Telegram-Init-Data','')
 if not BOT_TOKEN or not raw or len(raw)>8192:_bad('افتحه من تيليغرام.',401)
 pairs=parse_qsl(raw,keep_blank_values=True)
 if len(pairs)!=len({k for k,_ in pairs}):_bad('بيانات تيليغرام غير صالحة.',401)
 v=dict(pairs); got=v.pop('hash',None)
 try:auth_date=int(v.get('auth_date',''))
 except (TypeError,ValueError):_bad('بيانات تيليغرام غير صالحة.',401)
 now=int(time.time())
 if auth_date>now+60 or now-auth_date>MAX_INIT_DATA_AGE:_bad('انتهت صلاحية جلسة تيليغرام، أعد فتح التطبيق.',401)
 key=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest(); check='\n'.join(f'{k}={v[k]}'for k in sorted(v))
 if not got or not hmac.compare_digest(got,hmac.new(key,check.encode(),hashlib.sha256).hexdigest()):_bad('بيانات تيليغرام غير صالحة.',401)
 try:u=json.loads(v.get('user','{}'));uid=int(u['id'])
 except (KeyError,TypeError,ValueError,json.JSONDecodeError):_bad('بيانات المستخدم غير صالحة.',401)
 if not isinstance(u,dict) or uid<=0 or db.is_banned(uid):_bad('لا تملك صلاحية الوصول.',403)
 db.update_user_activity(uid)
 return uid,u
def own(uid,cid):
 x=db.get_stage_content_by_id(cid); s=db.get_user_stage(uid)
 if not x or not s or x[1]!=s[0]: _bad('المحتوى غير متاح.',404)
 return x
def pack(x):
 i,st,k,ch,typ,f,n,t,d,_,no=x; sub=db.get_stage_subject(st,k);return {'id':i,'chapter':ch,'title':d or n or f'محتوى #{no}','text':t or '','has_file':bool(f)and typ!='text','subject':sub[0]if sub else k}
async def index(r):
    """Version assets by content so Telegram WebViews receive each deployment."""
    html = (WEB_ROOT / 'index.html').read_text(encoding='utf-8')
    for asset in ('js/app.js', 'css/style.css'):
        version = hashlib.sha256((WEB_ROOT / asset).read_bytes()).hexdigest()[:16]
        html = html.replace(f'/static/{asset}', f'/static/{asset}?v={version}')
    return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-cache'})
async def health(r):return web.json_response({'ok':True})
async def me(r):
 uid,u=ident(r);s=db.get_user_stage(uid);return web.json_response({'registered':bool(s),'stage':s[0]if s else None,'full_name':s[1]if s else u.get('first_name',''),'notifications':db.notifications_enabled(uid)})
async def register(r):
 uid,u=ident(r);d=await _body(r);n=str(d.get('full_name','')).strip();st=d.get('stage')
 if db.get_user_stage(uid):_bad('المرحلة مسجلة مسبقاً ولا يمكن تغييرها من الموقع.',409)
 if len(n)>120 or len(n.split())<2 or st not in(1,2,3,4):_bad('تحقق من الاسم والمرحلة.')
 db.add_user(uid,u.get('username'),u.get('first_name',''),u.get('last_name',''));db.set_user_stage(uid,n,st);return web.json_response({'ok':True})
async def subjects(r):
 uid,_=ident(r);s=db.get_user_stage(uid);c=r.match_info['c'];names={'subjects':'المواد الدراسية','explanations':'الشروحات','lab':'المختبر','monthly_exams':'الامتحانات الشهرية','final_exams':'الامتحانات النهائية','research':'قسم الرابعة'}
 if not s or c not in names or(c=='research'and s[0]!=4):_bad('القسم غير متاح.', 404)
 return web.json_response({'title':names[c],'items':[{'name':n,'key':k}for n,k in db.get_stage_subjects_by_category(s[0],c)]})
async def chapters(r):
 uid,_=ident(r);s=db.get_user_stage(uid);x=db.get_stage_subject(s[0],r.match_info['k'])if s else None
 if not x:_bad('المادة غير متاحة.', 404)
 return web.json_response({'name':x[0],'chapters':[1,2,3,4,5,6]})
async def content_list(r):
 uid,_=ident(r);_limit(uid,'content');s=db.get_user_stage(uid);k=r.query.get('subject','');c=_integer(r.query.get('chapter'));sub=db.get_stage_subject(s[0],k)if s else None
 if not sub or c not in range(1,7):_bad('طلب محتوى غير صالح.')
 ic={'video':'🎥','document':'📄','photo':'🖼️','text':'📝','audio':'🎵','voice':'🎤'};return web.json_response({'subject':sub[0],'items':[{'id':x[0],'title':x[2]or f'محتوى #{x[4]}','icon':ic.get(x[1],'📎')}for x in db.get_stage_content(s[0],k,c)]})
async def content(r):
 uid,_=ident(r);_limit(uid,'content');x=own(uid,_integer(r.match_info['i']));db.mark_content_viewed(uid,x[0]);d=pack(x);d['favorite']=db.is_favorite(uid,x[0]);return web.json_response(d)
async def send(r):
 uid,_=ident(r);x=own(uid,_integer(r.match_info['i']));from utils import ContentSender
 if not await ContentSender.send_stage_single_content(uid,x,db.is_admin(uid)):_bad('تعذر إرسال المحتوى إلى المحادثة.', 502)
 return web.json_response({'ok':True})
async def favorites(r):
 uid,_=ident(r);s=db.get_user_stage(uid);return web.json_response({'items':[pack(x)for x in db.get_user_favorites(uid)if s and x[1]==s[0]]})
async def favorite(r):uid,_=ident(r);_limit(uid,'favorites');i=_integer(r.match_info['i']);own(uid,i);return web.json_response({'enabled':db.toggle_favorite(uid,i)})
async def progress(r):uid,_=ident(r);return web.json_response(db.get_user_progress(uid))
async def notifications(r):uid,_=ident(r);return web.json_response({'enabled':db.toggle_notifications(uid)})
async def search(r):
 uid,_=ident(r);_limit(uid,'search');s=db.get_user_stage(uid);q=' '.join(r.query.get('q','').split())
 if not s or not 2<=len(q)<=120:_bad('اكتب من حرفين إلى 120 حرفاً للبحث.')
 return web.json_response({'items':[{'id':x['id'],'title':x.get('description')or x.get('file_name')or x['content_type'],'subject':n}for x,n in db.search_content(q,s[0])]})
async def quiz(r):
 uid,_=ident(r);_limit(uid,'quiz');s=db.get_user_stage(uid);q=db.get_random_quiz(s[0])if s else None;return web.json_response({'quiz':{'id':q['id'],'question':q['question'],'options':q['options']}if q else None})
async def answer(r):
 uid,_=ident(r);_limit(uid,'quiz');s=db.get_user_stage(uid);q=db.get_quiz(_integer(r.match_info['i']));d=await _body(r);answer_index=d.get('answer')
 if not s or not q or q.get('stage')!=s[0] or not isinstance(answer_index,int) or isinstance(answer_index,bool) or answer_index not in range(len(q.get('options',[]))):_bad('إجابة الاختبار غير صالحة.')
 return web.json_response({'correct':bool(db.record_quiz_attempt(uid,q['id'],answer_index)),'correct_answer':q['options'][q['correct_index']]})
async def comments(r):
 uid,_=ident(r);i=_integer(r.match_info['i']);own(uid,i)
 if r.method=='POST':
  _limit(uid,'comments');t=str((await _body(r)).get('text','')).strip()
  if not t or len(t)>1000:_bad('طول التعليق غير صالح.')
  if not db.add_comment(uid,i,t,db.get_user_stage(uid)[0]):_bad('انتظر قليلاً قبل إضافة تعليق آخر.',429)
  return web.json_response({'ok':True})
 return web.json_response({'items':[{'id':x[0],'name':x[4]or x[5]or'طالب','text':x[2],'can_delete':x[1]==uid or db.is_admin(uid)}for x in db.get_comments_for_content(i)]})
async def delete_comment(r):uid,_=ident(r);_limit(uid,'comments');return web.json_response({'ok':db.delete_comment(_integer(r.match_info['i']),uid)})
async def support(r):
 uid,_=ident(r);_limit(uid,'support');s=db.get_user_stage(uid);t=str((await _body(r)).get('message','')).strip()
 if not s or not t or len(t)>2000:_bad('رسالة الدعم غير صالحة.')
 ticket_id=db.add_support_ticket(uid,t,s[0])
 recipients={DEVELOPER_ID}; stage_admin=db.get_support_admin(s[0])
 if stage_admin:recipients.add(stage_admin)
 for admin_id in recipients:
  try:await client.send_message(admin_id,f'📩 تذكرة من الموقع #{ticket_id}\n👤 {s[1]}\n🆔 {uid}\n\n{t}')
  except Exception:pass
 return web.json_response({'id':ticket_id})
async def ai(r):
 uid,_=ident(r);_limit(uid,'ai');s=db.get_user_stage(uid);q=str((await _body(r)).get('question','')).strip()
 if not s or not 3<=len(q)<=1500:_bad('السؤال غير صالح.')
 if not db.get_ai_enabled():return web.json_response({'answer':'⚠️ الذكاء الاصطناعي معطل حالياً.'})
 from ai_handler import explain_question;return web.json_response({'answer':await explain_question(uid,q,s[0])})
async def set_menu_button(token,url):
 async with ClientSession()as s:
  async with s.post(f'https://api.telegram.org/bot{token}/setChatMenuButton',json={'menu_button':{'type':'web_app','text':'Open','web_app':{'url':url}}})as r:d=await r.json()
 if not d.get('ok'):raise RuntimeError(d.get('description','تعذر إعداد زر Open'))
_runners=[]
@web.middleware
async def api_errors(request, handler):
 try:
  response = await handler(request)
  if request.path.startswith('/static/'):
   response.headers['Cache-Control'] = 'no-cache'
  return response
 except web.HTTPException as error:
  if request.path.startswith('/api/'):
   message = error.reason if error.reason else 'تعذر تنفيذ الطلب.'
   return web.json_response({'error':message}, status=error.status)
  raise
 except Exception:
  logging.exception('Unhandled Mini App request error: %s',request.path)
  if request.path.startswith('/api/'):
   return web.json_response({'error':'تعذر تنفيذ الطلب، حاول لاحقاً.'},status=500)
  raise
async def start_webapp():
 app=web.Application(middlewares=[api_errors],client_max_size=64*1024)
 app.router.add_static('/static/', WEB_ROOT, show_index=False)
 app.add_routes([web.get('/',index),web.get('/health',health),web.get('/api/me',me),web.post('/api/register',register),web.get('/api/subjects/{c}',subjects),web.get('/api/subjects/{k}/chapters',chapters),web.get('/api/content',content_list),web.get('/api/content/{i}',content),web.post('/api/content/{i}/send',send),web.get('/api/favorites',favorites),web.post('/api/favorites/{i}',favorite),web.get('/api/progress',progress),web.post('/api/notifications',notifications),web.get('/api/search',search),web.get('/api/quiz',quiz),web.post('/api/quiz/{i}/answer',answer),web.get('/api/content/{i}/comments',comments),web.post('/api/content/{i}/comments',comments),web.delete('/api/comments/{i}',delete_comment),web.post('/api/support',support),web.post('/api/ai',ai)])
 runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'0.0.0.0',MINI_APP_PORT).start();_runners.append(runner);return runner
