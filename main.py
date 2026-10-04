import sys
sys.dont_write_bytecode = True
import asyncio
from telethon import events
from styled_buttons import Button
from telethon.errors import MessageNotModifiedError
from telethon.tl.types import UpdateBotStopped
from datetime import datetime, timezone
from config import client, user_client, DEVELOPER_ID, loop, BOT_TOKEN, MINI_APP_URL, STORAGE_CHANNEL_ID
from database import db, IRAQ_TZ
from keyboards import Keyboards
from utils import ContentSender, ChannelSearch, check_subscription, send_subscription_message, content_upload_handler
from ai_handler import explain_question
from webapp import set_menu_button, start_webapp
import os
import time
import logging
from security import guard_event, content_access, action_limit, safe_send, RecipientUnavailableError
import telethon
from datetime import datetime
from database import IRAQ_TZ
from physics_facts import FACT_COUNT, load_facts, format_fact
# Validate the bundled library on startup, before serving requests.
load_facts()

async def handle_lab_experiment(event, parts, send=False):
    stage, key, course, exp = (int(parts[1]), parts[2], int(parts[3]), int(parts[4]))
    subject = await asyncio.to_thread(db.get_stage_subject, stage, key)
    is_admin = await asyncio.to_thread(db.is_admin, event.sender_id)
    profile = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    if not subject or subject[3] != 'lab' or (not is_admin and (not profile or profile[0] != stage)):
        await event.answer('المختبر غير متاح.', alert=True)
        return
    slot = await asyncio.to_thread(db.lab_slot, stage, course, exp)
    contents = await asyncio.to_thread(db.get_stage_content, stage, key, slot)
    if send:
        if not contents:
            await event.answer('لا يوجد محتوى مرفوع.', alert=True)
            return
        if len(contents) == 1:
            content = await asyncio.to_thread(db.get_stage_content_by_id, contents[0][0])
            await ContentSender.send_stage_single_content(event.chat_id, content, is_admin)
        else:
            await ContentSender.send_stage_content_list(event.chat_id, stage, key, slot, contents, is_admin)
        await event.answer()
        return
    buttons = []
    if contents:
        buttons.append([Button.inline('📩 إرسال المحتوى المرفوع', f'lab_send:{stage}:{key}:{course}:{exp}')])
    if is_admin:
        buttons.append([Button.inline('⬆️ رفع المحتوى', f'stage_content:add:{stage}:{key}:{slot}')])
    buttons.append([Button.inline('العودة للتجارب', f'lab_course:select:{stage}:{course}:{key}')])
    label = await asyncio.to_thread(db.chapter_label, stage, key, slot)
    await event.edit(f'{subject[0]} — {label}\n' + (f'المحتوى المرفوع: {len(contents)}' if contents else 'لم يرفع محتوى لهذه التجربة بعد.'), buttons=buttons)

async def handle_chapter_admin(event, data):
    if event.sender_id != DEVELOPER_ID:
        await event.answer('للمطور فقط.', alert=True)
        return
    action, stage, key = (data[0], int(data[1]), data[2])
    subject = await asyncio.to_thread(db.get_stage_subject, stage, key)
    if not subject or subject[3] in ('lab', 'archived_lab'):
        await event.answer('هذه المادة غير متاحة للإدارة.', alert=True)
        return
    if action == 'add':
        await asyncio.to_thread(set_pending_action, event.sender_id, 'chapter_add', {'stage': stage, 'key': key})
        await event.reply('أرسل اسم زر الفصل الجديد، مثل: الفصل السابع.', buttons=[[Button.inline('إلغاء', f'chapter_admin:cancel:{stage}:{key}')]])
        return
    if action == 'cancel':
        await asyncio.to_thread(clear_pending_action, event.sender_id)
    if action == 'remove':
        chapter = int(data[3])
        label = await asyncio.to_thread(db.chapter_label, stage, key, chapter)
        await event.edit(f'حذف زر «{label}»؟ سيُحفظ المحتوى الموجود دون مسحه.', buttons=[[Button.inline('تأكيد حذف الزر', f'chapter_admin:confirm:{stage}:{key}:{chapter}')], [Button.inline('إلغاء', f'chapter_admin:menu:{stage}:{key}:0')]])
        return
    if action == 'confirm':
        await asyncio.to_thread(db.remove_chapter, stage, key, int(data[3]), event.sender_id)
    chapters = await asyncio.to_thread(db.get_chapters, stage, key)
    page = max(0, int(data[3])) if action == 'menu' and len(data) > 3 else 0
    page = min(page, max(0, (len(chapters) - 1) // 15))
    buttons = [[Button.inline('➕ إضافة زر فصل', f'chapter_admin:add:{stage}:{key}')]]
    buttons.extend([[Button.inline('🗑 حذف ' + x['label'], f"chapter_admin:remove:{stage}:{key}:{x['id']}")] for x in chapters[page * 15:(page + 1) * 15]])
    nav = []
    if page:
        nav.append(Button.inline('السابق', f'chapter_admin:menu:{stage}:{key}:{page - 1}'))
    if (page + 1) * 15 < len(chapters):
        nav.append(Button.inline('التالي', f'chapter_admin:menu:{stage}:{key}:{page + 1}'))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline('العودة للمادة', f'stage_subject:{stage}:{key}')])
    await event.edit(f'إدارة أزرار {subject[0]} — {len(chapters)} فصل', buttons=buttons)

async def process_chapter_add(event, data):
    if event.sender_id != DEVELOPER_ID:
        await event.reply('للمطور فقط.')
        return
    label = (event.raw_text or '').strip()
    try:
        await asyncio.to_thread(db.add_chapter, data['stage'], data['key'], label, event.sender_id)
    except ValueError:
        await asyncio.to_thread(set_pending_action, event.sender_id, 'chapter_add', data)
        await event.reply('اسم الزر يجب أن يكون بين 1 و80 حرفاً.')
        return
    await event.reply('تمت إضافة الزر.', buttons=[[Button.inline('فتح المادة', f"stage_subject:{data['stage']}:{data['key']}")]])

async def handle_quiz_admin(event, data):
    from quiz_tools import KINDS
    actor = event.sender_id
    if not await asyncio.to_thread(db.is_admin, actor):
        await event.answer('للأدمن فقط.', alert=True)
        return
    action = data[0]
    if action == 'cancel':
        await asyncio.to_thread(clear_pending_action, actor)
        await event.edit('تم إلغاء إضافة الاختبار.')
        return
    if action in ('start', 'manage'):
        await asyncio.to_thread(clear_pending_action, actor)
        stages = [i for i in (1, 2, 3, 4) if await asyncio.to_thread(db.can_manage_stage, actor, i)]
        target = 'stage' if action == 'start' else 'list'
        await event.edit('اختر المرحلة:', buttons=[[Button.inline(f'المرحلة {i}', f'quiz_admin:{target}:{i}:0')] for i in stages] + [[Button.inline('الرئيسية', 'main:home')]])
        return
    if action in ('delete', 'confirm_delete'):
        quiz_id, page = int(data[1]), max(0, int(data[2]))
        quiz = await asyncio.to_thread(db.get_quiz, quiz_id)
        if not quiz or not await asyncio.to_thread(db.can_manage_stage, actor, quiz['stage']):
            await event.answer('السؤال غير موجود أو ليس ضمن صلاحيتك.', alert=True)
            return
        if action == 'delete':
            if not quiz['enabled']:
                await event.answer('تم حذف هذا السؤال بالفعل.', alert=True)
                return
            await event.edit(f"هل تريد حذف السؤال #{quiz_id}؟\n\n{quiz['question']}", parse_mode=None, buttons=[[Button.inline('تأكيد الحذف', f'quiz_admin:confirm_delete:{quiz_id}:{page}')], [Button.inline('إلغاء', f"quiz_admin:list:{quiz['stage']}:{page}")]])
            return
        try:
            deleted = await asyncio.to_thread(db.delete_quiz, quiz_id, actor)
        except PermissionError:
            await event.answer('تغيرت صلاحيتك. لا يمكن الحذف.', alert=True)
            return
        await event.answer('تم حذف السؤال.' if deleted else 'تم حذف السؤال بالفعل.', alert=True)
        await handle_quiz_admin(event, ['list', str(quiz['stage']), str(page)])
        return
    if action in ('stage', 'subject', 'type', 'list'):
        stage = int(data[1])
        if not await asyncio.to_thread(db.can_manage_stage, actor, stage):
            await event.answer('لا تملك صلاحية هذه المرحلة.', alert=True)
            return
        if action == 'list':
            page = max(0, int(data[2])) if len(data) > 2 else 0
            rows = await asyncio.to_thread(db.get_manageable_quizzes, actor, stage, page * 5, 6)
            if not rows and page:
                await handle_quiz_admin(event, ['list', str(stage), '0'])
                return
            buttons = [[Button.inline(f"🗑 #{q['id']} {q['question'][:40]}", f"quiz_admin:delete:{q['id']}:{page}")] for q in rows[:5]]
            nav = []
            if page: nav.append(Button.inline('السابق', f'quiz_admin:list:{stage}:{page-1}'))
            if len(rows) > 5: nav.append(Button.inline('التالي', f'quiz_admin:list:{stage}:{page+1}'))
            if nav: buttons.append(nav)
            buttons += [[Button.inline('إضافة سؤال', f'quiz_admin:stage:{stage}')], [Button.inline('العودة', 'quiz_admin:manage')]]
            await event.edit(f'إدارة أسئلة المرحلة {stage} — الصفحة {page+1}\nاختر السؤال لحذفه.' if rows else 'لا توجد أسئلة في هذه المرحلة.', buttons=buttons)
            return
        if action == 'stage':
            subjects = await asyncio.to_thread(db.get_stage_subjects_by_category, stage, 'subjects')
            await event.edit('اختر مادة الاختبار:', buttons=[[Button.inline(name, f'quiz_admin:subject:{stage}:{key}')] for name, key in subjects] + [[Button.inline('إلغاء', 'quiz_admin:cancel')]])
            return
        key = data[2]
        if not await asyncio.to_thread(db.get_stage_subject, stage, key):
            await event.answer('المادة غير موجودة.', alert=True)
            return
        if action == 'subject':
            await event.edit('اختر نوع السؤال:', buttons=[[Button.inline(label, f"quiz_admin:type:{stage}:{key}:{ {'multiple_choice':'mc','true_false':'tf','fill_blank':'blank'}[kind]}")] for kind, label in KINDS.items()] + [[Button.inline('إلغاء', 'quiz_admin:cancel')]])
            return
        kind = {'mc':'multiple_choice','tf':'true_false','blank':'fill_blank'}.get(data[3], data[3])
        if kind not in KINDS:
            await event.answer('نوع السؤال غير صالح.', alert=True)
            return
        await asyncio.to_thread(set_pending_action, actor, 'quiz_question', {'stage': stage, 'key': key, 'question_type': kind})
        await event.reply('أرسل السؤال (حتى 1000 حرف).' + (' ضع ___ مكان الفراغ.' if kind == 'fill_blank' else ''), buttons=[[Button.inline('إلغاء', 'quiz_admin:cancel')]])
        return
    if action == 'correct':
        pending = await asyncio.to_thread(pending_user_actions.get, actor, {})
        if pending.get('action') != 'quiz_correct':
            await event.answer('انتهت عملية إضافة الاختبار.', alert=True)
            return
        await save_quiz_draft(event, pending['data'], int(data[1]))

async def ask_quiz_correct(event, data):
    await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_correct', data)
    await event.reply('اختر الإجابة الصحيحة:', buttons=[[Button.inline(f'{i+1}. {x}', f'quiz_admin:correct:{i}')] for i, x in enumerate(data['options'])] + [[Button.inline('إلغاء', 'quiz_admin:cancel')]])

async def process_quiz_input(event, action, data):
    from quiz_tools import normalize_answer
    if not await asyncio.to_thread(db.can_manage_stage, event.sender_id, data['stage']):
        await event.reply('تم إلغاء صلاحية إضافة الاختبار.')
        return
    text = (event.raw_text or '').strip()
    if action == 'quiz_question':
        if not 1 <= len(text) <= 1000:
            await asyncio.to_thread(set_pending_action, event.sender_id, action, data)
            await event.reply('السؤال يجب أن يكون بين 1 و1000 حرف.')
            return
        data['question'] = text
        kind = data.get('question_type', 'multiple_choice')
        if kind == 'true_false':
            data['options'] = ['صح', 'خطأ']
            await ask_quiz_correct(event, data)
        elif kind == 'fill_blank':
            await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_blank_answers', data)
            await event.reply('أرسل الإجابة الصحيحة للفراغ. يمكنك إضافة بدائل مقبولة، كل بديل في سطر مستقل (حتى 10 بدائل، و100 حرف لكل بديل).')
        else:
            await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_options', data)
            await event.reply('أرسل من خيارين إلى ستة خيارات مختلفة، كل خيار في سطر مستقل (حتى 100 حرف للخيار).')
        return
    if action == 'quiz_blank_answers':
        answers = [x.strip() for x in text.splitlines() if x.strip()]
        if not 1 <= len(answers) <= 10 or any(len(x) > 100 or not normalize_answer(x) for x in answers):
            await asyncio.to_thread(set_pending_action, event.sender_id, action, data)
            await event.reply('أرسل من إجابة واحدة إلى 10 بدائل غير فارغة، كل إجابة حتى 100 حرف في سطر مستقل.')
            return
        data['accepted_answers'], data['options'] = answers, []
        await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_correct', data)
        await save_quiz_draft(event, data, 0)
        return
    if action == 'quiz_options':
        options = [x.strip() for x in text.splitlines() if x.strip()]
        if not 2 <= len(options) <= 6 or any(len(x) > 100 for x in options) or len(set(options)) != len(options):
            await asyncio.to_thread(set_pending_action, event.sender_id, action, data)
            await event.reply('أرسل من خيارين إلى ستة خيارات مختلفة، كل خيار في سطر مستقل، حتى 100 حرف.')
            return
        data['options'] = options
        await ask_quiz_correct(event, data)
        return
    try: index = int(text) - 1
    except ValueError: index = -1
    if index not in range(len(data.get('options', []))):
        await asyncio.to_thread(set_pending_action, event.sender_id, action, data)
        await event.reply('اختر الإجابة من الأزرار أو أرسل رقمها.')
        return
    await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_correct', data)
    await save_quiz_draft(event, data, index)

async def process_quiz_answer_blank(event, data):
    from quiz_tools import correct_label
    text = (event.raw_text or '').strip()
    if not 1 <= len(text) <= 100:
        await asyncio.to_thread(set_pending_action, event.sender_id, 'quiz_answer_blank', data)
        await event.reply('أرسل إجابة بين 1 و100 حرف.')
        return
    result = await asyncio.to_thread(db.record_quiz_attempt, event.sender_id, data['quiz_id'], text)
    quiz = await asyncio.to_thread(db.get_quiz, data['quiz_id'])
    message = 'انتهت المحاولة أو تم حذف السؤال. ابدأ سؤالاً جديداً.' if result is None else ('✅ إجابة صحيحة' if result else f'❌ إجابة غير صحيحة\nالصحيح: {correct_label(quiz)}')
    await event.reply(message, parse_mode=None, buttons=[[Button.inline('🧪 سؤال آخر', 'learning:quiz')], [Button.inline('العودة', 'main:home')]])

async def save_quiz_draft(event, data, index):
    if not await asyncio.to_thread(db.is_admin, event.sender_id):
        await event.reply('للأدمن فقط.')
        return
    try:
        quiz_id = await asyncio.to_thread(db.complete_quiz_draft, event.sender_id, index)
    except (ValueError, KeyError, PermissionError):
        await event.reply('انتهت عملية الإضافة أو تغيرت الصلاحية. ابدأ إضافة اختبار جديد.')
        return
    await event.reply(f"تمت إضافة الاختبار #{quiz_id} لطلاب المرحلة {data['stage']}.", buttons=[[Button.inline('إضافة اختبار آخر', 'quiz_admin:start')], [Button.inline('إدارة الأسئلة', 'quiz_admin:manage')]])

async def initialize_user_session():
    if user_client:
        try:
            if not user_client.is_connected():
                from utils import connect_assistant
                await connect_assistant()
                print('✅ جلسة المستخدم للبحث جاهزة')
            else:
                print('✅ جلسة المستخدم للبحث نشطة بالفعل')
            return True
        except Exception as e:
            print('تعذر تنفيذ العملية. حاول لاحقاً.')
            return False
    return True

def get_stage_subjects_count(stage):
    """الحصول على عدد المواد لكل مرحلة"""
    counts = {1: {'subjects': 10, 'explanations': 10, 'lab': 3, 'exams': 10}, 2: {'subjects': 12, 'explanations': 12, 'lab': 3, 'exams': 12}, 3: {'subjects': 8, 'explanations': 8, 'lab': 2, 'exams': 8}, 4: {'subjects': 7, 'explanations': 7, 'lab': 1, 'exams': 7, 'research': 3}}
    return counts.get(stage, counts[4])

@client.on(events.NewMessage(pattern='^/(?:start|admin)(?:@\\w+)?(?:\\s|$)'))
async def handle_start(event):
    if not await guard_event(event):
        return
    user_id = event.sender_id
    if await asyncio.to_thread(db.is_banned, user_id):
        await event.reply('⛔ تم حظرك من استخدام هذا البوت.')
        return
    is_admin = await asyncio.to_thread(db.is_admin, user_id) or user_id == DEVELOPER_ID
    user_stage = await asyncio.to_thread(db.get_user_stage, user_id)
    if not user_stage:
        await event.reply('👤 مرحباً بك! يرجى إرسال اسمك الثلاثي:')
        await asyncio.to_thread(set_pending_action, user_id, 'register_name')
        return
    stage = user_stage[0]
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
    welcome_msg = f'أهلاً {user_stage[1]}\n• بـوت المـرحلة الـ{stage_name} •\n• ڪل شيء هـنا لـوجه الله •\n• لا تـنسونا من دعـائڪم •\n'
    if '/admin' in event.raw_text and (not is_admin):
        await event.reply('⛔ ليس لديك صلاحية المسؤول')
        return
    await event.reply(welcome_msg, buttons=await asyncio.to_thread(Keyboards.main_menu, user_id, stage))

async def process_user_name(event):
    """Process user's full name and show stage selection"""
    user_id = event.sender_id
    full_name = event.raw_text.strip()
    if len(full_name.split()) < 2 or len(full_name) > 120:
        await event.reply('❌ يرجى إرسال الاسم الثلاثي بشكل صحيح (مثال: عباس غزوان عبد):')
        await asyncio.to_thread(set_pending_action, user_id, 'register_name')
        return
    user = await event.get_sender()
    await asyncio.to_thread(db.add_user, user_id=user_id, username=user.username, first_name=user.first_name, last_name=user.last_name)
    await asyncio.to_thread(set_pending_action, user_id, 'register_stage', {'full_name': full_name})
    await event.reply(f'👤 شكراً {full_name}\nالآن اختر مرحلتك الدراسية:', buttons=await asyncio.to_thread(Keyboards.stage_selection_menu, full_name))

@client.on(events.CallbackQuery)
async def handle_callbacks(event):
    if not await guard_event(event):
        return
    'Handle all callbacks from inline buttons'
    user_id = event.sender_id
    raw = getattr(event, 'data', None)
    try:
        data = raw.decode('utf-8') if raw is not None else ''
    except Exception:
        data = str(raw)
    if await asyncio.to_thread(db.is_banned, user_id):
        await event.answer('⛔ تم حظرك من استخدام هذا البوت', alert=True)
        return
    is_admin = await asyncio.to_thread(db.is_admin, user_id)
    is_developer = user_id == DEVELOPER_ID
    parts = data.split(':')
    requested_stage = None
    try:
        if parts[0] in ('stage_1', 'stage_2', 'stage_3', 'stage_4'):
            requested_stage = int(parts[0].split('_')[1])
        elif parts[0] in ('stage_exams', 'stage_subject', 'stage_chapter', 'content_page', 'lab_experiment', 'lab_send'):
            requested_stage = int(parts[1])
        elif parts[0] == 'lab_course' and len(parts) > 2:
            requested_stage = int(parts[2])
        profile = await asyncio.to_thread(db.get_user_stage, user_id)
        if requested_stage is not None and (requested_stage not in (1, 2, 3, 4) or (not is_admin and (not profile or profile[0] != requested_stage))):
            await event.answer('المحتوى غير متاح لمرحلتك.', alert=True)
            return
    except (ValueError, IndexError):
        await event.answer('زر غير صالح.', alert=True)
        return
    if is_admin and requested_stage is not None and not await asyncio.to_thread(db.can_manage_stage,user_id,requested_stage):
        is_admin = False
    if is_admin and parts and (parts[0] == 'admin'):
        await asyncio.to_thread(db.log_action, user_id, data)
    try:
        if data.startswith('register_stage'):
            await handle_register_stage(event, parts)
        elif data == 'check_subscription':
            await handle_check_subscription(event)
        elif data == 'delete_message':
            await handle_delete_message(event)
        elif parts[0] in ('stage_1', 'stage_2', 'stage_3', 'stage_4', 'stage_exams', 'stage_subject', 'stage_chapter'):
            await handle_stage_callbacks(event, parts, is_admin)
        elif len(parts) > 1 and parts[1] == 'physics_info':
            await handle_physics_info_stage(event, int(parts[0].split('_')[1]))
        elif len(parts) > 1 and parts[1] == 'ai':
            await handle_ai_button_stage(event, int(parts[0].split('_')[1]))
        elif parts[0] == 'content_page':
            stage, chapter, page = (int(parts[1]), int(parts[3]), int(parts[4]))
            contents = await asyncio.to_thread(db.get_stage_content, stage, parts[2], chapter)
            await ContentSender.send_stage_content_list(event.chat_id, stage, parts[2], chapter, contents, is_admin, page=page)
            await event.answer()
        elif parts[0] == 'stage_content':
            await handle_stage_content(event, parts[1], parts[2:], is_admin)
        elif parts[0] == 'main':
            await handle_main_menu(event, parts[1], is_admin)
        elif parts[0] == 'admin':
            await handle_admin_management(event, parts[1], parts[2:], is_developer)
        elif parts[0] == 'support':
            await handle_support(event, parts[1])
        elif parts[0] == 'support_reply':
            await handle_support_reply_button(event, parts[1])
        elif parts[0] == 'search_page':
            await handle_search_page(event, parts[1], parts[2:])
        elif parts[0] in ('lab_experiment', 'lab_send'):
            await handle_lab_experiment(event, parts, send=parts[0] == 'lab_send')
        elif parts[0] == 'chapter_admin':
            await handle_chapter_admin(event, parts[1:])
        elif parts[0] == 'quiz_admin':
            await handle_quiz_admin(event, parts[1:])
        elif parts[0] == 'lab_course':
            await handle_lab_course(event, parts[1], parts[2:])
        elif parts[0] == 'content_comments':
            await handle_content_comments(event, parts[1], parts[2:])
        elif parts[0] == 'content_manage':
            await handle_content_management(event, parts[1], parts[2:], is_admin)
        elif parts[0] == 'learning':
            await handle_learning(event, parts[1], parts[2:], is_admin)
        elif parts[0] == 'none':
            await event.answer('⚠️ لا يوجد محتوى', alert=True)
    except MessageNotModifiedError:
        await event.answer()
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)
        print(f'Callback error: {e}')
        import traceback
        traceback.print_exc()

async def handle_learning(event, action, data, is_admin=False):
    user_id = event.sender_id
    user_stage = await asyncio.to_thread(db.get_user_stage, user_id)
    stage = user_stage[0] if user_stage else 4
    if action == 'favorite':
        content_id = int(data[0])
        if not await content_access(event, content_id):
            return
        if not await asyncio.to_thread(db.get_stage_content_by_id, content_id):
            await event.answer('❌ المحتوى غير موجود', alert=True)
            return
        enabled = await asyncio.to_thread(db.toggle_favorite, user_id, content_id)
        await event.answer('✅ أضيف إلى المفضلة' if enabled else 'تمت إزالته من المفضلة', alert=True)
    elif action == 'favorites':
        favorites = await asyncio.to_thread(db.get_user_favorites, user_id)
        if not favorites:
            await event.edit('⭐ لا يوجد محتوى في المفضلة.', buttons=[[Button.inline('العودة', 'main:home')]])
            return
        buttons = []
        for item in favorites[:30]:
            description = item[8] or item[6] or f'محتوى #{item[10]}'
            buttons.append([Button.inline(f'⭐ {description[:35]}', f'stage_content:view:{item[0]}')])
        buttons.append([Button.inline('العودة', 'main:home')])
        await event.edit(f'⭐ المفضلة ({len(favorites)}):', buttons=buttons)
    elif action == 'progress':
        stats = await asyncio.to_thread(db.get_user_progress, user_id)
        message = f"📈 تقدمك الدراسي:\n\n👁 محتوى تمت مشاهدته: {stats['viewed']}\n🔁 مجموع المشاهدات: {stats['total_views']}\n🧪 الاختبارات: {stats['quiz_attempts']}\n✅ الإجابات الصحيحة: {stats['quiz_correct']} ({stats['quiz_percent']}%)"
        await event.edit(message, buttons=[[Button.inline('🏆 لوحة المتفوقين', 'learning:leaderboard')], [Button.inline('العودة', 'main:home')]])
    elif action == 'notifications':
        if data and data[0] == 'toggle':
            enabled = await asyncio.to_thread(db.toggle_notifications, user_id)
        else:
            enabled = await asyncio.to_thread(db.notifications_enabled, user_id)
        status = 'مفعلة ✅' if enabled else 'معطلة ❌'
        toggle = '❌ تعطيل' if enabled else '✅ تفعيل'
        await event.edit(f'🔔 إشعارات المحتوى: {status}', buttons=[[Button.inline(toggle, 'learning:notifications:toggle')], [Button.inline('العودة', 'main:home')]])
    elif action == 'quiz':
        pending = await asyncio.to_thread(pending_user_actions.get, user_id, {})
        if pending.get('action') == 'quiz_answer_blank':
            await asyncio.to_thread(clear_pending_action, user_id)
        quiz = await asyncio.to_thread(db.get_random_quiz, stage, user_id)
        if not quiz:
            await event.edit('🧪 لا توجد أسئلة لمرحلتك حاليًا.', buttons=[[Button.inline('العودة', 'main:home')]])
            return
        buttons = [[Button.inline(option, f"learning:answer:{quiz['id']}:{index}")] for index, option in enumerate(quiz['options'])]
        buttons.append([Button.inline('العودة', 'main:home')])
        suffix = ''
        if quiz.get('question_type') == 'fill_blank':
            await asyncio.to_thread(set_pending_action, user_id, 'quiz_answer_blank', {'quiz_id': quiz['id']})
            suffix = '\n\nأرسل إجابة الفراغ في رسالة (حتى 100 حرف). إذا تعددت الفراغات، اكتب الإجابة كاملة بالترتيب المحدد في السؤال.'
        await event.edit(f"🧪 {quiz['question']}{suffix}", buttons=buttons, parse_mode=None)
    elif action == 'answer':
        quiz_id, answer_index = (int(data[0]), int(data[1]))
        correct = await asyncio.to_thread(db.record_quiz_attempt, user_id, quiz_id, answer_index)
        quiz = await asyncio.to_thread(db.get_quiz, quiz_id)
        if correct is None:
            await event.answer('انتهت المحاولة أو تم حذف السؤال. ابدأ سؤالاً جديداً.', alert=True)
            return
        from quiz_tools import correct_label
        result = '✅ إجابة صحيحة' if correct else f"❌ إجابة غير صحيحة\nالصحيح: {correct_label(quiz)}"
        await event.edit(result, parse_mode=None, buttons=[[Button.inline('🧪 سؤال آخر', 'learning:quiz')], [Button.inline('🏆 لوحة المتفوقين', 'learning:leaderboard')], [Button.inline('العودة', 'main:home')]])
    elif action == 'leaderboard':
        rows = await asyncio.to_thread(db.get_quiz_leaderboard, stage)
        if not rows:
            message = '🏆 لا توجد نتائج بعد.'
        else:
            medals = ['🥇', '🥈', '🥉']
            lines = []
            for index, (_, name, correct, attempts) in enumerate(rows):
                rank = medals[index] if index < 3 else f'{index + 1}.'
                lines.append(f'{rank} {name}: {correct}/{attempts}')
            message = '🏆 لوحة المتفوقين:\n\n' + '\n'.join(lines)
        await event.edit(message, buttons=[[Button.inline('العودة', 'main:home')]])
    elif action == 'search':
        await asyncio.to_thread(set_pending_action, user_id, 'content_search', {'stage': stage})
        await event.reply('🔎 أرسل كلمة أو عبارة للبحث في محتوى مرحلتك:')

async def display_results_page(event, session, page, edit=False):
    results = session['results']
    pages = max(1, (len(results) + 4) // 5)
    page = max(0, min(int(page), pages - 1))
    lines = [f"نتائج البحث عن: {session['query']}", f'النتائج: {len(results)} — الصفحة {page + 1}/{pages}']
    buttons = []
    for i, result in enumerate(results[page * 5:(page + 1) * 5], page * 5 + 1):
        lines.append(f"{i}. {result['channel_title']}\n{result.get('message_text', '')[:180]}")
        if result.get('content_id'):
            buttons.append([Button.inline(f'فتح النتيجة {i}', f"stage_content:view:{result['content_id']}")])
        elif result.get('message_link'):
            buttons.append([Button.url(f'فتح النتيجة {i}', result['message_link'])])
    if not results:
        lines.append('لا توجد نتائج مطابقة.')
    if session.get('status'):
        lines.append(session['status'])
    nav = []
    if page:
        nav.append(Button.inline('السابق', f'search_page:{page - 1}'))
    if page + 1 < pages:
        nav.append(Button.inline('التالي', f'search_page:{page + 1}'))
    if nav:
        buttons.append(nav)
    buttons.extend([[Button.inline('بحث جديد', f"stage_{session['stage']}:search")], [Button.inline('الرئيسية', 'main:home')]])
    send = event.edit if edit else event.reply
    await send('\n\n'.join(lines), buttons=buttons, parse_mode=None, link_preview=False)

async def process_content_search(event, stage):
    query = (event.raw_text or '').strip()
    if not 2 <= len(query) <= 120:
        await event.reply('اكتب كلمة بحث بين حرفين و120 حرفاً.')
        return
    local = await asyncio.to_thread(db.search_content, query, stage, limit=None)
    results = [{'content_id': c['id'], 'channel_title': name, 'message_text': c.get('description') or c.get('file_name') or (c.get('text') or '')[:200] or c['content_type']} for c, name in local]
    session = {'query': query, 'stage': stage, 'results': results, 'at': time.time()}
    expired = [uid for uid, value in search_sessions.items() if time.time() - value.get('at', 0) > 1800]
    for uid in expired:
        search_sessions.pop(uid, None)
    search_sessions[event.sender_id] = session
    await display_results_page(event, session, 0)

async def handle_register_stage(event, data):
    """Handle stage selection for new user"""
    try:
        stage = int(data[1])
        pending = await asyncio.to_thread(pending_user_actions.get, event.sender_id, {})
        if pending.get('action') != 'register_stage' or time.time() - pending.get('at', 0) > 900 or await asyncio.to_thread(db.get_user_stage, event.sender_id) or (stage not in (1, 2, 3, 4)):
            await event.answer('أعد بدء التسجيل بالأمر /start.', alert=True)
            return
        full_name = pending['data']['full_name']
        await asyncio.to_thread(clear_pending_action, event.sender_id)
        user_id = event.sender_id
        await asyncio.to_thread(db.set_user_stage, user_id, full_name, stage)
        if user_id != DEVELOPER_ID:
            try:
                user = await event.get_sender()
            except Exception:
                user = None
            username_text = f'@{user.username}' if user and getattr(user, 'username', None) else 'بدون يوزرنيم'
            new_user_info = f'👤 مستخدم جديد:\n🆔 ID: {user_id}\n👤 الاسم: {full_name}\n🎓 المرحلة: {stage}\n📌 اليوزر: {username_text}'
            try:
                await client.send_message(DEVELOPER_ID, new_user_info)
            except Exception as e:
                print(f'Failed sending new-user notification: {e}')
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        welcome_msg = f'أهلاً {full_name}\n• بـوت المـرحلة الـ{stage_name} •\n• ڪل شيء هـنا لـوجه الله •\n• لا تـنسونا من دعـائڪم •\n'
        await event.edit(welcome_msg, buttons=await asyncio.to_thread(Keyboards.main_menu, user_id, stage))
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_check_subscription(event):
    user_id = event.sender_id
    if await check_subscription(user_id):
        user_stage = await asyncio.to_thread(db.get_user_stage, user_id)
        if user_stage:
            stage = user_stage[0]
            buttons = await asyncio.to_thread(Keyboards.main_menu, user_id, stage)
            await event.edit('✅ تم التحقق من الاشتراك بنجاح!', buttons=buttons)
        else:
            await event.edit('✅ تم التحقق من الاشتراك بنجاح! يرجى إكمال التسجيل.')
    else:
        await event.answer('⚠️ لم يتم الاشتراك بعد، يرجى المحاولة مرة أخرى', alert=True)

async def handle_delete_message(event):
    """Delete the current message"""
    try:
        await event.delete()
    except:
        await event.answer('تم الإغلاق')

async def handle_search_page(event, page, data):
    session = search_sessions.get(event.sender_id)
    if not session or time.time() - session.get('at', 0) > 1800:
        await event.answer('انتهت جلسة البحث. ابحث من جديد.', alert=True)
        return
    profile = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    if not await asyncio.to_thread(db.is_admin, event.sender_id) and (not profile or profile[0] != session['stage']):
        await event.answer('صلاحية مرفوضة.', alert=True)
        return
    await display_results_page(event, session, int(page), edit=True)
    await event.answer()

async def handle_ai_button_stage(event, stage):
    """معالج زر الذكاء الاصطناعي"""
    try:
        ai_enabled = await asyncio.to_thread(db.get_ai_enabled)
        if not ai_enabled:
            await event.answer('❌ الذكاء الاصطناعي معطل حالياً من قبل الإدارة', alert=True)
            return
        await asyncio.to_thread(set_pending_action, event.sender_id, 'ai_question', {'stage': stage})
        await event.reply('🤖 المساعد الذكي:\n\nأرسل سؤالك الآن وسيتم الرد عليك بواسطة الذكاء الاصطناعي.')
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def process_ai_question(event, stage):
    """معالجة سؤال المستخدم للذكاء الاصطناعي"""
    try:
        question = (event.raw_text or '').strip()
        if len(question) < 3:
            await event.reply('❌ يرجى إرسال سؤال أوضح.')
            await asyncio.to_thread(set_pending_action, event.sender_id, 'ai_question', {'stage': stage})
            return
        progress_msg = await event.reply('🤖 جارِ توليد الإجابة...')
        answer = await explain_question(event.sender_id, question, stage)
        await progress_msg.edit(answer)
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

def physics_fact_buttons(stage):
    return [[Button.inline('⚛️ معلومة أخرى', f'stage_{stage}:physics_info')],
            [Button.inline('العودة', f'stage_{stage}:home')]]

async def deliver_physics_fact(user_id, stage, daily=False):
    reservation = await asyncio.to_thread(db.reserve_physics_fact, user_id)
    if reservation is None:
        return False
    try:
        await safe_send(user_id, format_fact(reservation['fact'], daily),
                        buttons=physics_fact_buttons(stage), parse_mode=None)
    except RecipientUnavailableError:
        await asyncio.shield(asyncio.to_thread(db.cancel_physics_fact, user_id, reservation['token']))
        logging.info('Physics delivery skipped: recipient unavailable, user=%s', user_id)
        return False
    except BaseException:
        await asyncio.shield(asyncio.to_thread(db.cancel_physics_fact, user_id, reservation['token']))
        raise
    if not await asyncio.to_thread(db.complete_physics_fact, user_id, reservation['token']):
        raise RuntimeError('Physics send reservation lost')
    if daily: await asyncio.to_thread(db.mark_daily_physics,user_id)
    return True

async def handle_physics_info_stage(event, stage):
    """Send a local fact, sharing persistent history with the daily worker."""
    profile = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    if not await asyncio.to_thread(db.is_admin, event.sender_id) and (not profile or profile[0] != stage):
        await event.answer('المعلومة غير متاحة لهذه المرحلة.', alert=True)
        return
    if not await action_limit(event.sender_id, 'physics_info', 6, 60):
        await event.answer('انتظر قليلاً قبل طلب معلومة جديدة.', alert=True)
        return
    try:
        sent = await deliver_physics_fact(event.sender_id, stage)
        await event.answer('✅ تم إرسال المعلومة' if sent else 'جارٍ إرسال المعلومة السابقة، انتظر قليلاً.')
    except Exception:
        logging.exception('Local physics fact delivery failed')
        await event.answer('تعذر إرسال المعلومة. حاول مرة أخرى.', alert=True)

async def handle_lab_course(event, part1, part2):
    if part1 != 'select' or len(part2) < 2:
        await event.answer('اختيار غير صالح.', alert=True)
        return
    stage, course = (int(part2[0]), int(part2[1]))
    profile = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    if not await asyncio.to_thread(db.is_admin, event.sender_id) and (not profile or profile[0] != stage):
        await event.answer('المختبر غير متاح لمرحلتك.', alert=True)
        return
    key = part2[2] if len(part2) > 2 else 'nuclear_physics_lab' if stage == 4 else None
    if not key:
        await event.edit('اختر المختبر أولاً:', buttons=await asyncio.to_thread(Keyboards.stage_category_menu, stage, 'lab'))
        return
    subject = await asyncio.to_thread(db.get_stage_subject, stage, key)
    if not subject or subject[3] != 'lab' or course not in (1, 2):
        await event.answer('المختبر غير متاح.', alert=True)
        return
    count = await asyncio.to_thread(db.lab_experiment_count, stage)
    ordinals = ['الأولى', 'الثانية', 'الثالثة', 'الرابعة', 'الخامسة', 'السادسة']
    buttons = [[Button.inline('التجربة ' + ordinals[exp - 1], f'lab_experiment:{stage}:{key}:{course}:{exp}')] for exp in range(1, count + 1)]
    buttons.append([Button.inline('العودة للكورسات', f'stage_subject:{stage}:{key}')])
    await event.edit(f"{subject[0]} — الكورس {('الأول' if course == 1 else 'الثاني')}:", buttons=buttons)

async def handle_stage_callbacks(event, parts, is_admin):
    """Handle all stage-related callbacks"""
    if parts[0] == 'stage_1' or parts[0] == 'stage_2' or parts[0] == 'stage_3' or (parts[0] == 'stage_4'):
        stage = int(parts[0].split('_')[1])
        action = parts[1] if len(parts) > 1 else 'home'
        await handle_stage_main_menu(event, stage, action)
    elif parts[0] == 'stage_exams':
        stage = int(parts[1])
        exam_type = parts[2] if len(parts) > 2 else ''
        await handle_exam_type_selection(event, stage, exam_type, is_admin)
    elif parts[0] == 'stage_subject':
        stage = int(parts[1])
        subject_key = parts[2]
        await handle_stage_subject(event, stage, subject_key, is_admin)
    elif parts[0] == 'stage_chapter':
        stage = int(parts[1])
        subject_key = parts[2]
        chapter_num = int(parts[3])
        await handle_stage_chapter(event, stage, subject_key, chapter_num, is_admin)

async def handle_stage_main_menu(event, stage, action):
    """Handle main menu for specific stage"""
    if action == 'home':
        user_stage = await asyncio.to_thread(db.get_user_stage, event.sender_id)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        display_name = user_stage[1] if user_stage else 'بك'
        welcome_msg = f'أهلاً {display_name}\n• بـوت المـرحلة الـ{stage_name} •\n• ڪل شيء هـنا لـوجه الله •\n• لا تـنسونا من دعـائڪم •\n'
        await event.edit(welcome_msg, buttons=await asyncio.to_thread(Keyboards.main_menu, event.sender_id, stage))
    elif action == 'search':
        await handle_stage_search(event, stage)
    elif action == 'lab':
        await event.edit('اختر كورس المختبر:', buttons=await asyncio.to_thread(Keyboards.lab_courses_menu, stage))
    elif action == 'exams':
        await event.edit('اختر نوع الامتحانات:', buttons=await asyncio.to_thread(Keyboards.exam_type_menu, stage))
    elif action == 'research' and stage == 4:
        await event.edit('قسم البحث - اختر القسم:', buttons=await asyncio.to_thread(Keyboards.stage_category_menu, stage, 'research', await asyncio.to_thread(db.is_admin, event.sender_id)))
    elif action == 'ai':
        await handle_ai_button_stage(event, stage)
    elif action == 'physics_info':
        await handle_physics_info_stage(event, stage)
    else:
        category_map = {'subjects': 'subjects', 'explanations': 'explanations', 'lab': 'lab', 'research': 'research'}
        if action not in category_map:
            await event.answer('❌ القسم غير موجود', alert=True)
            return
        stage_counts = get_stage_subjects_count(stage)
        category_count = stage_counts.get(category_map[action], 0)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        category_names = {'subjects': 'المواد الدراسية', 'explanations': 'الشروحات', 'lab': 'المختبر', 'research': 'قسم البحث'}
        await event.edit(f'اختر من {category_names[action]} (المرحلة {stage_name}) - العدد: {category_count}:', buttons=await asyncio.to_thread(Keyboards.stage_category_menu, stage, category_map[action], await asyncio.to_thread(db.is_admin, event.sender_id)))

async def handle_exam_type_selection(event, stage, exam_type, is_admin):
    """عرض مواد الامتحانات الشهرية أو النهائية."""
    category_by_type = {'monthly': ('monthly_exams', 'الامتحانات الشهرية'), 'final': ('final_exams', 'الامتحانات النهائية')}
    selected = category_by_type.get(exam_type)
    if not selected:
        await event.answer('❌ نوع الامتحان غير موجود', alert=True)
        return
    category, title = selected
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
    count = len(await asyncio.to_thread(db.get_stage_subjects_by_category, stage, category))
    await event.edit(f'اختر من {title} (المرحلة {stage_name}) - العدد: {count}:', buttons=await asyncio.to_thread(Keyboards.stage_category_menu, stage, category, is_admin))

async def handle_stage_search(event, stage):
    """Handle search functionality for specific stage"""
    await event.reply('🔍 أدخل الكلمة للبحث في قنوات البحث المضافة لمرحلتك:')
    await asyncio.to_thread(set_pending_action, event.sender_id, 'stage_search', {'stage': stage})

async def process_stage_search_query(event, stage):
    query = (event.raw_text or '').strip()
    if not 2 <= len(query) <= 120:
        await event.reply('اكتب كلمة بحث بين حرفين و120 حرفاً.')
        return
    progress = await event.reply('جار البحث في نتائج قنوات مرحلتك ضمن حدود الوقت والنتائج…')
    results = []
    channels = await asyncio.to_thread(db.get_search_channels, stage)
    status = 'لا توجد قنوات بحث مضافة لمرحلتك. يمكن للأدمن إضافتها من إدارة قنوات البحث.'
    if channels:
        try:
            results, status = await asyncio.wait_for(ChannelSearch.search_in_telegram_channels(query,channels[:20],stage,limit_per_channel=30),45)
        except asyncio.TimeoutError:
            status = 'تجاوز البحث الوقت المحدد، حاول بكلمة أدق.'
    session = {'query': query, 'stage': stage, 'results': results, 'at': time.time(), 'status': status}
    expired = [uid for uid, value in search_sessions.items() if time.time() - value.get('at', 0) > 1800]
    for uid in expired:
        search_sessions.pop(uid, None)
    search_sessions[event.sender_id] = session
    try:
        await progress.delete()
    except Exception:
        pass
    await display_results_page(event, session, 0)

async def get_search_suggestions(query, stage):
    """تقديم اقتراحات بحث ذكية"""
    suggestions = []
    stage_keywords = {1: ['أولى', 'الصف الأول'], 2: ['ثانية', 'الصف الثاني'], 3: ['ثالثة', 'الصف الثالث'], 4: ['رابعة', 'الصف الرابع']}
    stage_words = stage_keywords.get(stage, [])
    for word in stage_words:
        suggestions.append(f'{query} {word}')
    educational_terms = ['شرح', 'امتحان', 'ملخص', 'تمارين', 'حلول']
    for term in educational_terms:
        suggestions.append(f'{term} {query}')
        suggestions.append(f'{query} {term}')
    return suggestions[:5]

async def handle_stage_subject(event, stage, subject_key, is_admin):
    subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
    profile = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    if not subject or subject[3] == 'archived_lab' or (not is_admin and (not profile or profile[0] != stage)):
        await event.answer('المادة غير متاحة.', alert=True)
        return
    if subject[3] == 'lab':
        await event.edit(f'{subject[0]} — اختر الكورس:', buttons=await asyncio.to_thread(Keyboards.lab_courses_menu, stage, subject_key))
        return
    await event.edit(f'{subject[0]} — اختر الفصل:', buttons=await asyncio.to_thread(Keyboards.stage_chapters_menu, stage, subject_key, is_admin, event.sender_id == DEVELOPER_ID))

async def handle_stage_chapter(event, stage, subject_key, chapter_num, is_admin):
    """Handle chapter selection for specific stage"""
    subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
    if not await asyncio.to_thread(db.is_admin, event.sender_id) and (not await asyncio.to_thread(db.get_user_stage, event.sender_id) or (await asyncio.to_thread(db.get_user_stage, event.sender_id))[0] != stage):
        await event.answer('المادة غير متاحة لمرحلتك.', alert=True)
        return
    if not subject:
        await event.answer('❌ المادة غير موجودة', alert=True)
        return
    if not await asyncio.to_thread(db.is_valid_chapter, stage, subject_key, chapter_num):
        await event.answer('الفصل أو التجربة غير متاحة.', alert=True)
        return
    if subject[3] == 'lab':
        count = await asyncio.to_thread(db.lab_experiment_count, stage)
        await handle_lab_experiment(event, ['lab_experiment', str(stage), subject_key, str((chapter_num - 1) // count + 1), str((chapter_num - 1) % count + 1)])
        return
    content_list = await asyncio.to_thread(db.get_stage_content, stage, subject_key, chapter_num)
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
    if content_list:
        await ContentSender.send_stage_content_list(event.chat_id, stage, subject_key, chapter_num, content_list, is_admin)
        await event.answer('📂 جارِ تحميل قائمة المحتوى...')
    else:
        await event.edit(f'▫️ {subject[0]} - الفصل {chapter_num} (المرحلة {stage_name})\n\n⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.', buttons=await asyncio.to_thread(Keyboards.stage_chapter_empty_menu, stage, subject_key, chapter_num, is_admin))

async def handle_stage_content(event, action, data, is_admin):
    """Handle stage content management"""
    try:
        if action == 'view':
            content_id = int(data[0])
            await handle_view_content(event, content_id, is_admin)
        elif action == 'add':
            if not is_admin:
                await event.answer('⛔ صلاحية مرفوضة', alert=True)
                return
            stage = int(data[0])
            subject_key = data[1]
            chapter_num = int(data[2])
            await handle_add_content(event, stage, subject_key, chapter_num)
        elif action == 'delete':
            if not is_admin:
                await event.answer('⛔ صلاحية مرفوضة', alert=True)
                return
            content_id = int(data[0])
            await handle_delete_content(event, content_id, is_admin)
        elif action == 'upload_type':
            if not is_admin:
                await event.answer('⛔ صلاحية مرفوضة', alert=True)
                return
            stage = int(data[0])
            subject_key = data[1]
            chapter_num = int(data[2])
            content_type = data[3]
            await handle_upload_type_selection(event, stage, subject_key, chapter_num, content_type)
        elif action == 'cancel_upload':
            await handle_cancel_upload(event)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)
        print(f'Error in handle_stage_content: {e}')

async def handle_upload_type_selection(event, stage, subject_key, chapter_num, content_type):
    """بدء عملية رفع المحتوى بعد اختيار النوع"""
    try:
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        if not subject:
            await event.answer('❌ المادة غير موجودة', alert=True)
            return
        message = await content_upload_handler.start_upload_session(event.sender_id, stage, subject_key, chapter_num, content_type)
        buttons = [[Button.inline('❌ إلغاء الرفع', f'stage_content:cancel_upload')]]
        await event.edit(message, buttons=buttons)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_cancel_upload(event):
    """إلغاء عملية الرفع"""
    result = await asyncio.to_thread(content_upload_handler.cancel_upload, event.sender_id)
    await event.answer(result, alert=True)
    user_stage = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    stage = user_stage[0] if user_stage else 4
    await event.edit('✅ تم إلغاء عملية الرفع', buttons=await asyncio.to_thread(Keyboards.main_menu, event.sender_id, stage))

async def handle_content_comments(event, action, data):
    """معالج التعليقات على المحتوى"""
    try:
        if action == 'view':
            content_id = int(data[0])
            await handle_view_comments(event, content_id, int(data[1]) if len(data) > 1 else 0)
        elif action == 'add':
            content_id = int(data[0])
            await event.reply('📝 اكتب تعليقك على هذا المحتوى:')
            await asyncio.to_thread(set_pending_action, event.sender_id, 'add_comment', {'content_id': content_id})
        elif action == 'delete':
            comment_id = int(data[0])
            content_id = int(data[1]) if len(data) > 1 else None
            await handle_delete_comment(event, comment_id, content_id)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_view_comments(event, content_id, page=0):
    if not await content_access(event, content_id):
        return
    'عرض جميع تعليقات المحتوى'
    try:
        comments = await asyncio.to_thread(db.get_comments_for_content, content_id)
        if not comments:
            await event.edit('💬 لا توجد تعليقات على هذا المحتوى بعد')
            return
        message = f'💬 التعليقات على هذا المحتوى ({len(comments)}):\n\n'
        buttons = []
        page = max(0, min(page, max(0, (len(comments) - 1) // 10)))
        for comment_id, user_id, text, comment_date, first_name, username in comments[page * 10:(page + 1) * 10]:
            user_display = f'@{username}' if username else first_name or f'المستخدم {user_id}'
            message += f'👤 {user_display}\n'
            message += f"📝 {text[:100]}{('...' if len(text) > 100 else '')}\n"
            message += f'⏰ {comment_date[:10]}\n\n'
            if event.sender_id == user_id or await asyncio.to_thread(db.is_admin, event.sender_id):
                buttons.append([Button.inline(f'🗑️ حذف التعليق #{comment_id}', f'content_comments:delete:{comment_id}:{content_id}')])
        nav = []
        if page > 0:
            nav.append(Button.inline('السابق', f'content_comments:view:{content_id}:{page - 1}'))
        if (page + 1) * 10 < len(comments):
            nav.append(Button.inline('التالي', f'content_comments:view:{content_id}:{page + 1}'))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline('➕ إضافة تعليق', f'content_comments:add:{content_id}')])
        await event.edit(message, buttons=buttons)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def process_add_comment(event, content_id):
    if not await content_access(event, content_id):
        return
    'معالجة إضافة تعليق جديد'
    try:
        if not event.raw_text:
            await event.reply('❌ يرجى إدخال نص التعليق')
            return
        content_data = await asyncio.to_thread(db.get_stage_content_by_id, content_id)
        if not content_data:
            await event.reply('❌ المحتوى غير موجود')
            return
        stage = content_data[1]
        comment_id = await asyncio.to_thread(db.add_comment, event.sender_id, content_id, event.raw_text, stage)
        if comment_id:
            await event.reply(f'✅ تم إضافة تعليقك بنجاح (#{comment_id})')
        else:
            await event.reply('⚠️ يرجى الانتظار قبل إضافة تعليق آخر (الحد الأدنى 10 ثوان بين التعليقات)')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def handle_delete_comment(event, comment_id, content_id):
    """حذف تعليق معين"""
    try:
        if await asyncio.to_thread(db.delete_comment, comment_id, event.sender_id):
            await event.answer(f'✅ تم حذف التعليق بنجاح', alert=True)
            if content_id:
                await handle_view_comments(event, content_id)
        else:
            await event.answer('❌ لم تتمكن من حذف هذا التعليق (يجب أن تكون المؤلف أو أدمن)', alert=True)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_view_content(event, content_id, is_admin):
    if not await content_access(event, content_id):
        return
    'عرض محتوى معين'
    try:
        content_data = await asyncio.to_thread(db.get_stage_content_by_id, content_id)
        if not content_data:
            await event.answer('❌ المحتوى غير موجود', alert=True)
            return
        sent = await ContentSender.send_stage_single_content(event.chat_id, content_data, is_admin=is_admin)
        if sent:
            await asyncio.to_thread(db.mark_content_viewed, event.sender_id, content_id)
            await event.answer('✅ تم عرض المحتوى')
        else:
            await event.answer('❌ تعذر عرض المحتوى', alert=True)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_add_content(event, stage, subject_key, chapter_num):
    """بدء عملية إضافة محتوى جديد"""
    try:
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        if not subject:
            await event.answer('❌ المادة غير موجودة', alert=True)
            return
        await event.edit(f"📁 اختر نوع المحتوى الذي تريد إضافته لـ:\n📚 {subject[0]} - الفصل {chapter_num}\n\nالمرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]}", buttons=await asyncio.to_thread(Keyboards.content_type_selection_menu, stage, subject_key, chapter_num))
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_delete_content(event, content_id, is_admin):
    """حذف محتوى"""
    try:
        if not is_admin:
            await event.answer('⛔ صلاحية مرفوضة', alert=True)
            return
        content_data = await asyncio.to_thread(db.get_stage_content_by_id, content_id)
        if content_data and not await asyncio.to_thread(db.can_manage_stage,event.sender_id,content_data[1]):
            await event.answer('لا تملك صلاحية إدارة هذه المرحلة.',alert=True); return
        if not content_data:
            await event.answer('❌ المحتوى غير موجود', alert=True)
            return
        if await asyncio.to_thread(db.delete_stage_content, content_id, event.sender_id):
            await event.answer('✅ تم حذف المحتوى بنجاح', alert=True)
            stage = content_data[1]
            subject_key = content_data[2]
            chapter_num = content_data[3]
            content_list = await asyncio.to_thread(db.get_stage_content, stage, subject_key, chapter_num)
            if content_list:
                await event.edit(f'📂 محتوى الفصل {chapter_num}:\n✅ تم حذف المحتوى بنجاح', buttons=await asyncio.to_thread(Keyboards.stage_content_list_menu, stage, subject_key, chapter_num, content_list, is_admin))
            else:
                subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
                await event.edit(f'▫️ {subject[0]} - الفصل {chapter_num}\n\n✅ تم حذف المحتوى بنجاح\n⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.', buttons=await asyncio.to_thread(Keyboards.stage_chapter_empty_menu, stage, subject_key, chapter_num, is_admin))
        else:
            await event.answer('❌ فشل في حذف المحتوى', alert=True)
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def handle_content_management(event,action,data,is_admin):
    if not is_admin:
        await event.answer('⛔ صلاحية مرفوضة',alert=True);return
    if action=='type_select':
        await handle_upload_type_selection(event,int(data[0]),data[1],int(data[2]),data[3])
    elif action=='skip_description':
        content_id=int(data[0])
        content=await asyncio.to_thread(db.get_stage_content_by_id,content_id)
        if not content or not await asyncio.to_thread(db.can_manage_stage,event.sender_id,content[1]):
            await event.answer('صلاحية مرفوضة',alert=True);return
        await asyncio.to_thread(db.update_content_description,content_id,'')
        await event.answer('تم حفظ الوصف.')

search_sessions = {}
from workflows import WorkflowMap
pending_user_actions = WorkflowMap('pending')

def set_pending_action(user_id, action, data=None):
    pending_user_actions[user_id] = {'action': action, 'data': data or {}, 'at': time.time()}

def pop_pending_action(user_id):
    item = pending_user_actions.pop(user_id, None)
    return item if item and time.time() - item.get('at', 0) < 900 else None

def clear_pending_action(user_id):
    pending_user_actions.pop(user_id, None)

@client.on(events.NewMessage(pattern='^/addquiz(?:@\\w+)?(?:\\s|$)'))
async def handle_add_quiz_command(event):
    if not await guard_event(event):
        return
    if not await asyncio.to_thread(db.is_admin, event.sender_id):
        await event.reply('⛔ صلاحية مرفوضة')
        return
    payload = (event.raw_text or '').partition(' ')[2]
    parts = [x.strip() for x in payload.split('|')]
    if not payload.strip():
        await event.reply('اختر مرحلة الاختبار:', buttons=[[Button.inline(f'المرحلة {i}', f'quiz_admin:stage:{i}')] for i in (1, 2, 3, 4)])
        return
    if len(parts) != 8:
        await event.reply('الصيغة:\n/addquiz المرحلة | subject_key | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الصحيح')
        return
    try:
        stage = int(parts[0])
        correct_index = int(parts[7]) - 1
        if stage not in (1, 2, 3, 4) or correct_index not in range(4):
            raise ValueError
    except ValueError:
        await event.reply('❌ المرحلة من 1 إلى 4، ورقم الإجابة من 1 إلى 4.')
        return
    subjects = await asyncio.to_thread(db.get_stage_subjects_by_category, stage, 'subjects')
    key = parts[1]
    if not await asyncio.to_thread(db.get_stage_subject, stage, key):
        key = next((k for name, k in subjects if name == key), key)
    try:
        quiz_id = await asyncio.to_thread(db.add_quiz, stage, key, parts[2], parts[3:7], correct_index, event.sender_id)
    except (ValueError, PermissionError):
        await event.reply('تحقق من المادة وطول السؤال والخيارات، أو استخدم زر إضافة اختبار.', buttons=[[Button.inline('إضافة اختبار من الأزرار', 'quiz_admin:start')]])
        return
    await asyncio.to_thread(db.log_action, event.sender_id, 'quiz_added', {'quiz_id': quiz_id})
    await event.reply(f'تمت إضافة السؤال #{quiz_id}')

@client.on(events.NewMessage(pattern='^/schedule(?:\\s|$)'))
async def handle_schedule_command(event):
    if not await guard_event(event):
        return
    if not await asyncio.to_thread(db.is_admin, event.sender_id):
        await event.reply('⛔ صلاحية مرفوضة')
        return
    payload = (event.raw_text or '').partition(' ')[2]
    parts = payload.split(maxsplit=1)
    if len(parts) != 2:
        await event.reply('الصيغة:\n/schedule CONTENT_ID YYYY-MM-DD HH:MM')
        return
    try:
        content_id = int(parts[0])
        publish_at = datetime.strptime(parts[1], '%Y-%m-%d %H:%M').replace(tzinfo=IRAQ_TZ)
        if not await asyncio.to_thread(db.get_stage_content_by_id, content_id):
            raise ValueError
    except ValueError:
        await event.reply('❌ تحقق من معرف المحتوى وصيغة التاريخ.')
        return
    schedule_id = await asyncio.to_thread(db.schedule_content, content_id, publish_at.isoformat(), event.sender_id)
    await asyncio.to_thread(db.log_action, event.sender_id, 'content_scheduled', {'schedule_id': schedule_id, 'content_id': content_id})
    await event.reply(f'✅ تمت جدولة المحتوى #{content_id} في {publish_at:%Y-%m-%d %H:%M}')

@client.on(events.NewMessage(pattern='^/adminhelp$'))
async def handle_admin_help_command(event):
    if not await guard_event(event):
        return
    if not await asyncio.to_thread(db.is_admin, event.sender_id):
        return
    await event.reply('🛠 أوامر التطوير:\n\n/addquiz المرحلة | subject_key | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الصحيح\n/schedule CONTENT_ID YYYY-MM-DD HH:MM\n/admin للإحصائيات والنسخ الاحتياطي')

@client.on(events.NewMessage)
async def handle_upload_messages(event):
    if not await guard_event(event):
        return
    'معالجة رسائل المستخدم التي تنتظر خطوة تالية.'
    user_id = event.sender_id
    if event.raw_text and event.raw_text.startswith('/'):
        return
    session = await asyncio.to_thread(content_upload_handler.get_user_session, user_id)
    if session:
        try:
            if session['step'] == 'description':
                next_message, error = await content_upload_handler.process_description(user_id, event.text)
                if error:
                    await event.reply(error)
                    return
                buttons = [[Button.inline('❌ إلغاء الرفع', f'stage_content:cancel_upload')]]
                await event.reply(next_message, buttons=buttons)
            elif session['step'] == 'content':
                result = await content_upload_handler.process_content(user_id, event)
                await event.reply(result)
                if result.startswith('✅'):
                    stage = session['stage']
                    subject_key = session['subject_key']
                    chapter_num = session['chapter_num']
                    content_list = await asyncio.to_thread(db.get_stage_content, stage, subject_key, chapter_num)
                    if content_list:
                        await ContentSender.send_stage_content_list(event.chat_id, stage, subject_key, chapter_num, content_list, True)
                    else:
                        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
                        await event.reply(f'▫️ {subject[0]} - الفصل {chapter_num}\n\n⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.', buttons=await asyncio.to_thread(Keyboards.stage_chapter_empty_menu, stage, subject_key, chapter_num, True))
        except Exception as e:
            await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')
            await asyncio.to_thread(content_upload_handler.cancel_upload, user_id)
        return
    pending = await asyncio.to_thread(pop_pending_action, user_id)
    if not pending:
        return
    action = pending['action']
    data = pending.get('data', {})
    if action not in ('broadcast',) and (not event.raw_text or event.raw_text.startswith('/')):
        await asyncio.to_thread(set_pending_action, user_id, action, data)
        return
    try:
        if action == 'chapter_add':
            await process_chapter_add(event, data)
        elif action in ('quiz_question', 'quiz_options', 'quiz_correct', 'quiz_blank_answers'):
            await process_quiz_input(event, action, data)
        elif action == 'quiz_answer_blank':
            await process_quiz_answer_blank(event, data)
        elif action == 'register_name':
            await process_user_name(event)
        elif action == 'stage_search':
            await process_stage_search_query(event, int(data['stage']))
        elif action == 'ai_question':
            await process_ai_question(event, int(data['stage']))
        elif action == 'content_search':
            await process_content_search(event, int(data['stage']))
        elif action == 'add_comment':
            await process_add_comment(event, int(data['content_id']))
        elif action == 'support_reply':
            await process_support_reply(event, int(data['ticket_id']), int(data['original_message_id']))
        elif action == 'support_ticket':
            await process_support_ticket(event, int(data['stage']))
        elif action == 'add_admin':
            await process_add_admin(event)
        elif action == 'broadcast':
            if event.raw_text and (not event.raw_text.startswith('/')) or event.file:
                await process_broadcast_message(event)
            else:
                await asyncio.to_thread(set_pending_action, user_id, action, data)
        elif action == 'add_channel':
            await process_add_channel(event)
        elif action == 'add_search_channel':
            await process_add_search_channel(event, int(data['stage']))
        elif action == 'ban_user':
            await process_ban_user(event)
        elif action == 'unban_user':
            await process_unban_user(event)
        elif action == 'add_physics_channel':
            await process_add_physics_channel(event)
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def handle_main_menu(event, action, is_admin):
    """Handle main menu callbacks"""
    user_stage = await asyncio.to_thread(db.get_user_stage, event.sender_id)
    stage = user_stage[0] if user_stage else 4
    if action == 'home':
        pending = await asyncio.to_thread(pending_user_actions.get, event.sender_id, {})
        if pending.get('action') == 'quiz_answer_blank':
            await asyncio.to_thread(clear_pending_action, event.sender_id)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        display_name = user_stage[1] if user_stage else 'بك'
        welcome_msg = f'أهلاً {display_name}\n• بـوت المـرحلة الـ{stage_name} •\n• ڪل شيء هـنا لـوجه الله •\n• لا تـنسونا من دعـائڪم •\n'
        await event.edit(welcome_msg, buttons=await asyncio.to_thread(Keyboards.main_menu, event.sender_id, stage))
    elif action == 'physics_info':
        await handle_physics_info_stage(event, stage)
    elif action == 'ai':
        await handle_ai_button_stage(event, stage)
    elif action == 'exams':
        await event.edit('اختر نوع الامتحانات:', buttons=await asyncio.to_thread(Keyboards.exam_type_menu, stage))
    else:
        category_map = {'subjects': 'subjects', 'explanations': 'explanations', 'lab': 'lab', 'research': 'research'}
        if action not in category_map:
            await event.answer('❌ القسم غير موجود', alert=True)
            return
        stage_counts = get_stage_subjects_count(stage)
        category_count = stage_counts.get(category_map[action], 0)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        category_names = {'subjects': 'المواد الدراسية', 'explanations': 'الشروحات', 'lab': 'المختبر', 'research': 'قسم البحث'}
        await event.edit(f'اختر من {category_names[action]} (المرحلة {stage_name}) - العدد: {category_count}:', buttons=await asyncio.to_thread(Keyboards.stage_category_menu, stage, category_map[action], is_admin))

async def handle_support_reply_button(event, ticket_id):
    """معالجة ضغط زر الرد على تذكرة الدعم"""
    try:
        if not (await asyncio.to_thread(db.is_admin, event.sender_id) or event.sender_id == DEVELOPER_ID):
            await event.answer('⛔ صلاحية مرفوضة', alert=True)
            return
        await event.reply(f'📩 أرسل ردك على تذكرة الدعم #{ticket_id}:')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'support_reply', {'ticket_id': int(ticket_id), 'original_message_id': event.message_id})
    except Exception as e:
        await event.answer('تعذر تنفيذ العملية. حاول لاحقاً.', alert=True)

async def process_support_reply(event, ticket_id, original_message_id):
    """معالجة رد الأدمن على تذكرة الدعم"""
    try:
        await asyncio.to_thread(db.queue_support_reply,int(ticket_id),event.sender_id,event.raw_text or '')
        await event.reply('✅ حُفظ الرد في قائمة الإرسال؛ تُغلق التذكرة بعد وصول الرد.')
    except PermissionError:await event.reply('لا تملك صلاحية الرد على هذه المرحلة.')
    except ValueError:await event.reply('التذكرة مغلقة أو قيد الرد، أو النص غير صالح.')


async def show_bot_blocks(event, category='blocked', page=0, edit=True):
    if event.sender_id != DEVELOPER_ID or not getattr(event, 'is_private', True):
        await event.reply('هذه القائمة للمطور في المحادثة الخاصة فقط.')
        return
    labels = {'blocked': 'حظر البوت / إيقافه', 'deactivated': 'حسابات محذوفة', 'unavailable': 'سبب غير محدد', 'returned': 'عادوا للتفاعل'}
    if category not in labels:
        await event.reply('تصنيف غير صالح.')
        return
    report = await asyncio.to_thread(db.get_bot_block_report, event.sender_id, category, page)
    def stamp(value):
        try: return datetime.fromisoformat(value).astimezone(IRAQ_TZ).strftime('%Y-%m-%d %H:%M')
        except (TypeError, ValueError): return 'غير مسجل'
    lines = ['🚷 متابعة حظر البوت', ' | '.join(f'{labels[key]}: {count}' for key, count in report['counts'].items()), f"{labels[category]} — الصفحة {report['page']+1}/{report['pages']}"]
    for item in report['items']:
        username = '@' + item['username'] if item['username'] else 'بدون يوزر'
        detail = f"👤 {item['name'][:80]}\nID: {item['user_id']} | {username}\nالمرحلة: {item['stage'] or 'غير مسجلة'}\nاكتشاف الحالة: {stamp(item['detected_at'])}"
        if item.get('action_at'): detail += f"\nوقت الإيقاف من تيليجرام: {stamp(item['action_at'])}"
        if category == 'returned': detail += f"\nالحالة السابقة: {labels.get(item['reason'], labels['unavailable'])}\nعودة الوصول: {stamp(item['returned_at'])}"
        lines.append(detail)
    if not report['items']: lines.append('لا توجد حالات مسجلة في هذا التصنيف.')
    lines.append('الأوقات بتوقيت بغداد. القائمة تضم الحالات التي رصدها البوت؛ لا تثبت حالة جميع المستخدمين الذين لم يصل تحديث عنهم أو لم تُجرّب مراسلتهم.')
    buttons = [[Button.inline(label, f'admin:bot_blocks:{key}:0') for key, label in list(labels.items())[:2]], [Button.inline(label, f'admin:bot_blocks:{key}:0') for key, label in list(labels.items())[2:]]]
    nav = []
    if report['page']: nav.append(Button.inline('السابق', f"admin:bot_blocks:{category}:{report['page']-1}"))
    if report['page']+1 < report['pages']: nav.append(Button.inline('التالي', f"admin:bot_blocks:{category}:{report['page']+1}"))
    if nav: buttons.append(nav)
    buttons += [[Button.inline('تحديث', f"admin:bot_blocks:{category}:{report['page']}")], [Button.inline('العودة', 'admin:manage')]]
    await (event.edit if edit else event.reply)('\n\n'.join(lines), buttons=buttons, parse_mode=None)

@client.on(events.Raw(types=UpdateBotStopped))
async def bot_stopped_update(update):
    if not isinstance(update, UpdateBotStopped): return
    when = update.date
    if isinstance(when, datetime):
        when = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    await asyncio.to_thread(db.set_delivery_blocked, update.user_id, update.stopped, 'blocked', when, update.qts)

async def handle_admin_management(event, action, data_list, is_developer):
    """Handle admin management callbacks"""
    if not is_developer or event.sender_id != DEVELOPER_ID:
        await event.answer('⛔ صلاحية مرفوضة', alert=True)
        return
    if action == 'manage':
        await event.edit('🔐 لوحة إدارة الأدمن:', buttons=await asyncio.to_thread(Keyboards.admin_management_menu))
    elif action == 'bot_blocks':
        await show_bot_blocks(event, data_list[0] if data_list else 'blocked', int(data_list[1]) if len(data_list) > 1 else 0)
    elif action == 'admin_section':
        await event.edit('👥 قسم إدارة الأدمنية:', buttons=await asyncio.to_thread(Keyboards.admin_section_menu))
    elif action == 'ban_section':
        await event.edit('🚫 قسم إدارة الحظر:', buttons=await asyncio.to_thread(Keyboards.ban_section_menu))
    elif action == 'stats_section':
        await event.edit('📊 قسم الإحصائيات - اختر التصنيف:', buttons=await asyncio.to_thread(Keyboards.stats_section_menu))
    elif action == 'user_stats':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        filter_type = data_list[0]
        if filter_type == 'all':
            await show_all_users_stats(event)
        else:
            stage = int(filter_type)
            await show_stage_users_stats(event, stage)
    elif action == 'detailed_stats':
        await show_detailed_statistics(event)
    elif action == 'learning_stats':
        stats = await asyncio.to_thread(db.get_learning_statistics)
        await event.edit(f"📊 إحصائيات التعلم:\n\n⭐ المفضلة: {stats['favorites']}\n👁 مشاهدات المحتوى: {stats['content_views']}\n👥 طلاب شاهدوا محتوى: {stats['unique_viewers']}\n🧪 الأسئلة: {stats['quizzes']}\n📝 المحاولات: {stats['quiz_attempts']}\n✅ الإجابات الصحيحة: {stats['correct_answers']}", buttons=[[Button.inline('العودة', 'admin:manage')]])
    elif action == 'backup':
        backup_file = await asyncio.to_thread(db.create_backup)
        await asyncio.to_thread(db.log_action, event.sender_id, 'backup_created', {'file': os.path.basename(backup_file)})
        await client.send_file(event.chat_id, backup_file, caption='💾 نسخة احتياطية لقاعدة البيانات', force_document=True)
        await event.answer('✅ تم إنشاء النسخة الاحتياطية')
    elif action == 'add':
        await event.reply('🔢 أرسل معرف المستخدم (ID) الذي تريد ترقيته إلى أدمن:')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'add_admin')
    elif action == 'remove':
        admins = await asyncio.to_thread(db.get_admins)
        if len(admins) <= 1:
            await event.answer('⚠️ لا يوجد أدمن لإزالتهم', alert=True)
            return
        buttons = []
        for admin_id, username, full_name in admins:
            if admin_id != DEVELOPER_ID:
                btn_text = f'➖ {full_name or username or admin_id}'
                buttons.append([Button.inline(btn_text, f'admin:remove_confirm:{admin_id}')])
        buttons.append([Button.inline('العودة', 'admin:admin_section')])
        await event.edit('اختر الأدمن الذي تريد إزالته:', buttons=buttons)
    elif action == 'remove_confirm':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        admin_id = int(data_list[0])
        admins = await asyncio.to_thread(db.get_admins)
        target_admin = next((a for a in admins if a[0] == admin_id), None)
        if not target_admin or admin_id == DEVELOPER_ID:
            await event.answer('❌ لا يمكن إزالة هذا الأدمن', alert=True)
            return
        buttons = [[Button.inline('✅ تأكيد الإزالة', f'admin:remove_execute:{admin_id}'), Button.inline('❌ إلغاء', 'admin:admin_section')]]
        await event.edit(f'⚠️ هل أنت متأكد من إزالة الأدمن:\nID: {admin_id}\nUsername: @{target_admin[1]}\nName: {target_admin[2]}', buttons=buttons)
    elif action == 'remove_execute':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        admin_id = int(data_list[0])
        if await asyncio.to_thread(db.remove_admin, admin_id):
            await event.answer('✅ تم إزالة الأدمن بنجاح', alert=True)
            await event.edit('✅ تم إزالة الأدمن بنجاح', buttons=await asyncio.to_thread(Keyboards.admin_section_menu))
        else:
            await event.answer('❌ فشل في إزالة الأدمن', alert=True)
    elif action == 'list':
        admins = await asyncio.to_thread(db.get_admins)
        message = '👥 قائمة الأدمن:\n\n'
        for admin_id, username, full_name in admins:
            role = ' (المطور)' if admin_id == DEVELOPER_ID else ''
            message += f"🔹 {full_name or 'بدون اسم'}\n"
            message += f'   👤 @{username}\n' if username else '   👤 بدون يوزرنيم\n'
            message += f'   🆔 {admin_id}{role}\n\n'
        await event.edit(message, buttons=await asyncio.to_thread(Keyboards.admin_section_menu))
    elif action == 'support_admin_stages':
        buttons = [[Button.inline('المرحلة الأولى', 'admin:support_admin_select:1'), Button.inline('المرحلة الثانية', 'admin:support_admin_select:2')], [Button.inline('المرحلة الثالثة', 'admin:support_admin_select:3'), Button.inline('المرحلة الرابعة', 'admin:support_admin_select:4')], [Button.inline('العودة', 'admin:admin_section')]]
        await event.edit('📩 اختر المرحلة لتعيين أدمن الدعم لها:', buttons=buttons)
    elif action == 'support_admin_select':
        if not data_list:
            await event.answer('❌ لم تُحدد المرحلة', alert=True)
            return
        stage = int(data_list[0])
        admins = [admin for admin in await asyncio.to_thread(db.get_admins) if admin[0] != DEVELOPER_ID]
        if not admins:
            await event.answer('⚠️ أضف أدمن أولاً', alert=True)
            return
        buttons = [[Button.inline(full_name or username or str(admin_id), f'admin:support_admin_set:{stage}:{admin_id}')] for admin_id, username, full_name in admins]
        buttons.append([Button.inline('العودة', 'admin:support_admin_stages')])
        await event.edit('📩 اختر أدمن الدعم لهذه المرحلة:', buttons=buttons)
    elif action == 'support_admin_set':
        if len(data_list) < 2:
            await event.answer('❌ بيانات التعيين غير مكتملة', alert=True)
            return
        stage, admin_id = (int(data_list[0]), int(data_list[1]))
        if await asyncio.to_thread(db.set_support_admin, stage, admin_id):
            admin = next((item for item in await asyncio.to_thread(db.get_admins) if item[0] == admin_id), None)
            admin_name = admin[2] or admin[1] or str(admin_id) if admin else str(admin_id)
            await event.edit(f"✅ تم تعيين {admin_name} لاستلام تذاكر المرحلة {['الأولى', 'الثانية', 'الثالثة', 'الرابعة'][stage - 1]}", buttons=await asyncio.to_thread(Keyboards.admin_section_menu))
        else:
            await event.answer('❌ تعذر تعيين أدمن الدعم', alert=True)
    elif action == 'new_users':
        new_users = await asyncio.to_thread(db.get_new_users)
        if not new_users:
            await event.answer('⚠️ لا يوجد مستخدمين جدد', alert=True)
            return
        message = '👤 المستخدمين الجدد:\n\n'
        for user_id, first_name, last_name, username in new_users:
            message += f'🔹 {first_name} {last_name}\n' if first_name or last_name else '🔹 مستخدم جديد\n'
            message += f'   👤 @{username}\n' if username else '   👤 بدون يوزرنيم\n'
            message += f'   🆔 {user_id}\n\n'
        await event.reply(message, buttons=await asyncio.to_thread(Keyboards.stats_section_menu))
    elif action == 'user_stats_old':
        user_count = await asyncio.to_thread(db.count_users)
        await event.answer(f'👥 عدد المستخدمين: {user_count}', alert=True)
    elif action == 'broadcast':
        await event.reply('📢 أرسل الرسالة التي تريد إذاعتها لجميع المستخدمين:')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'broadcast')
    elif action == 'channel_manage':
        await event.edit('📌 إدارة قنوات الاشتراك الإجباري:', buttons=await asyncio.to_thread(Keyboards.channel_management_menu))
    elif action == 'channel_add':
        await event.reply('📢 أرسل معرف القناة أو رابطها لإضافتها للاشتراك الإجباري (مثل @shahmplus أو https://t.me/shahmplus):')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'add_channel')
    elif action == 'channel_remove':
        if not data_list:
            await event.answer('❌ لم يتم تحديد القناة', alert=True)
            return
        record_id = int(data_list[0])
        if await asyncio.to_thread(db.remove_required_channel, record_id):
            await event.answer('✅ تم إزالة القناة الإلزامية بنجاح', alert=True)
            await event.edit('📌 إدارة قنوات الاشتراك الإجباري:', buttons=await asyncio.to_thread(Keyboards.channel_management_menu))
        else:
            await event.answer('❌ القناة غير موجودة', alert=True)
    elif action == 'search_channels':
        await event.edit('🔍 إدارة قنوات البحث:', buttons=await asyncio.to_thread(Keyboards.search_channels_management))
    elif action == 'add_search_channel':
        await event.edit('🔍 اختر المرحلة لإضافة قناة البحث لها:', buttons=await asyncio.to_thread(Keyboards.stage_selection_for_channel))
    elif action == 'add_search_channel_stage':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        stage = int(data_list[0])
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        await event.reply(f'🔍 أرسل معرف القناة أو رابطها لإضافتها للبحث (المرحلة {stage_name}):')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'add_search_channel', {'stage': stage})
    elif action == 'remove_search_channel':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        record_id = int(data_list[0])
        stage = int(data_list[1]) if len(data_list) > 1 else None
        channel_info = await asyncio.to_thread(db.get_search_channel_by_id, record_id)
        if channel_info and await asyncio.to_thread(db.remove_search_channel, record_id, stage):
            channel_title = channel_info[2]
            channel_username = channel_info[1]
            await event.answer(f'✅ تم إزالة القناة {channel_title} (@{channel_username}) بنجاح', alert=True)
            await event.edit(f'✅ تم إزالة القناة {channel_title} (@{channel_username}) بنجاح', buttons=await asyncio.to_thread(Keyboards.search_channels_management))
        else:
            await event.answer('❌ فشل في إزالة القناة أو القناة غير موجودة', alert=True)
    elif action == 'check_all_channels':
        channels = await asyncio.to_thread(db.get_search_channels)
        message = '**📊 تقرير حالة قنوات البحث:**\n\n'
        for i, channel in enumerate(channels, 1):
            has_access, status_msg = await ChannelSearch.check_channel_access(channel[1])
            status_icon = '✅' if has_access else '❌'
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][channel[3] - 1]
            message += f'{i}. {status_icon} {channel[2]} (@{channel[1]}) - المرحلة {stage_name}\n'
            message += f'   📝 {status_msg}\n\n'
        buttons = [[Button.inline('🔄 تحديث التقرير', 'admin:check_all_channels')], [Button.inline('العودة', 'admin:search_channels')]]
        await event.edit(message, buttons=buttons)
    elif action == 'search_stats':
        channels = await asyncio.to_thread(db.get_search_channels)
        stats_by_stage = {}
        for channel in channels:
            stage = channel[3]
            if stage not in stats_by_stage:
                stats_by_stage[stage] = 0
            stats_by_stage[stage] += 1
        message = '**📊 إحصائيات قنوات البحث:**\n\n'
        for stage in sorted(stats_by_stage.keys()):
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
            message += f'🎓 المرحلة {stage_name}: {stats_by_stage[stage]} قناة\n'
        message += f'\n**الإجمالي: {len(channels)} قناة بحث**'
        await event.answer(message, alert=True)
    elif action == 'select_stage':
        if not data_list:
            await event.edit('🔎 اختر المرحلة التي تريد استعراضها:', buttons=await asyncio.to_thread(Keyboards.admin_stage_selector))
            return
        try:
            stage = int(data_list[0])
        except:
            await event.answer('❌ خطأ في اختيار المرحلة', alert=True)
            return
        await event.edit(f"🔎 استعراض المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]}:", buttons=await asyncio.to_thread(Keyboards.main_menu, event.sender_id, stage))
    elif action == 'manage_comments':
        await event.edit('💬 إدارة التعليقات والاستفسارات:', buttons=await asyncio.to_thread(Keyboards.admin_comments_menu))
    elif action == 'ban_user':
        await event.reply('🚫 أرسل معرف المستخدم (ID) الذي تريد حظره:')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'ban_user')
    elif action == 'unban_user':
        await event.reply('✅ أرسل معرف المستخدم (ID) الذي تريد إلغاء حظره:')
        await asyncio.to_thread(set_pending_action, event.sender_id, 'unban_user')
    elif action == 'support_tickets':
        await event.edit('📩 تذاكر الدعم المفتوحة:', buttons=await asyncio.to_thread(Keyboards.support_tickets_menu))
    elif action == 'view_ticket':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        ticket_id = int(data_list[0])
        ticket_info = await asyncio.to_thread(db.get_ticket_info, ticket_id)
        if not ticket_info:
            await event.answer('❌ التذكرة غير موجودة', alert=True)
            return
        message = f"📩 تذكرة الدعم #{ticket_id}\n👤 المرسل: {ticket_info[3]} (@{ticket_info[4]})\n📅 التاريخ: {ticket_info[5]}\n🎓 المرحلة: {['أولى', 'ثانية', 'ثالثة', 'رابعة'][ticket_info[2] - 1]}\n\n💬 الرسالة:\n{ticket_info[1]}"
        buttons = [[Button.inline('📩 الرد على التذكرة', f'support_reply:{ticket_id}')], [Button.inline('العودة', 'admin:support_tickets')]]
        await event.reply(message, buttons=buttons)
    elif action == 'close_ticket':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        ticket_id = int(data_list[0])
        if await asyncio.to_thread(db.close_support_ticket, ticket_id, event.sender_id):
            await event.answer('✅ تم إغلاق التذكرة', alert=True)
            await event.reply(f'✅ تم إغلاق تذكرة الدعم #{ticket_id}', buttons=await asyncio.to_thread(Keyboards.admin_management_menu))
        else:
            await event.answer('❌ فشل في حذف التذكرة', alert=True)
    elif action == 'ai_settings':
        ai_enabled = await asyncio.to_thread(db.get_ai_enabled)
        status = '✅ مفعّل' if ai_enabled else '❌ معطّل'
        buttons = [[Button.inline('✅ تفعيل الذكاء الاصطناعي', 'admin:ai_toggle:1' if not ai_enabled else 'none'), Button.inline('❌ تعطيل الذكاء الاصطناعي', 'admin:ai_toggle:0' if ai_enabled else 'none')], [Button.inline('العودة', 'admin:manage')]]
        await event.edit(f'🤖 إعدادات الذكاء الاصطناعي:\n\nالحالة الحالية: {status}', buttons=buttons)
    elif action == 'ai_toggle':
        if not data_list:
            await event.answer('❌ خطأ في البيانات', alert=True)
            return
        enabled = int(data_list[0]) == 1
        await asyncio.to_thread(db.set_ai_enabled, enabled)
        status = '✅ تم تفعيل' if enabled else '✅ تم تعطيل'
        await event.answer(f'{status} الذكاء الاصطناعي', alert=True)
        ai_enabled = await asyncio.to_thread(db.get_ai_enabled)
        status = '✅ مفعّل' if ai_enabled else '❌ معطّل'
        buttons = [[Button.inline('✅ تفعيل الذكاء الاصطناعي', 'admin:ai_toggle:1' if not ai_enabled else 'none'), Button.inline('❌ تعطيل الذكاء الاصطناعي', 'admin:ai_toggle:0' if ai_enabled else 'none')], [Button.inline('العودة', 'admin:manage')]]
        await event.edit(f'🤖 إعدادات الذكاء الاصطناعي:\n\nالحالة الحالية: {status}', buttons=buttons)
    elif action in ('physics_settings', 'physics_add_channel', 'physics_remove_channel'):
        await event.edit(
            f'📚 مكتبة المعلومات الفيزيائية المحلية\n\nعدد المعلومات: {FACT_COUNT}\n'
            '400 معلومة مفاهيمية و600 مثال حسابي قصير في 100 موضوع.\n'
            'الطلب المباشر والإرسال اليومي يستخدمان المكتبة نفسها، دون قناة خارجية.\n'
            'لا تتكرر المعلومة للطالب حتى يمر على المجموعة كاملة.\n'
            'الإرسال اليومي يحترم إعدادات إشعارات الطالب.',
            buttons=[[Button.inline('العودة', 'admin:manage')]])

async def show_all_users_stats(event):
    """عرض جميع مستخدمين البوت"""
    users = await asyncio.to_thread(db.get_users_by_stage)
    len(users)
    if not users:
        await event.edit('❌ لا يوجد مستخدمين مسجلين بعد')
        return
    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]
    await display_users_page(event, pages, 0, 'all')

async def show_stage_users_stats(event, stage):
    """عرض مستخدمين مرحلة محددة"""
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
    users = await asyncio.to_thread(db.get_users_by_stage, stage)
    len(users)
    if not users:
        await event.edit(f'❌ لا يوجد طلاب مسجلين في المرحلة {stage_name}')
        return
    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]
    await display_users_page(event, pages, 0, stage)

async def display_users_page(event, pages, page_num, filter_type):
    """عرض صفحة من قائمة المستخدمين"""
    if page_num >= len(pages):
        page_num = 0
    if not pages:
        await event.edit('لا يوجد مستخدمون.', buttons=[[Button.inline('العودة', 'admin:stats_section')]])
        return
    page_num = max(0, page_num)
    users = pages[page_num]
    total_users = sum((len(p) for p in pages))
    if filter_type == 'all':
        title = '👥 **جميع مستخدمين البوت**'
    else:
        stage = int(filter_type)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        title = f'🎓 **طلاب المرحلة {stage_name}**'
    message = f'{title}\n'
    message += f'📊 **الإجمالي:** {total_users} مستخدم\n'
    message += f'📄 **الصفحة:** {page_num + 1}/{len(pages)}\n'
    message += '━' * 30 + '\n\n'
    for i, user in enumerate(users, page_num * 10 + 1):
        user_id = user['user_id']
        full_name = user['full_name'] or f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        username = user.get('username', '')
        stage_num = user['stage']
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage_num - 1]
        date_joined = user.get('date_joined', '')
        if date_joined:
            date_joined = date_joined[:10]
        banned = '🔴 محظور' if user.get('is_banned', False) else '🟢 نشط'
        message += f'**{i}. {full_name}**\n'
        message += f'   🆔 `{user_id}`\n'
        if username:
            message += f'   📌 @{username}\n'
        message += f'   🎓 المرحلة {stage_name}\n'
        message += f'   📅 تاريخ التسجيل: {date_joined}\n'
        message += f'   {banned}\n\n'
    buttons = []
    nav_buttons = []
    if page_num > 0:
        nav_buttons.append(Button.inline('⬅️ السابق', f'admin:users_page:{filter_type}:{page_num - 1}'))
    if page_num < len(pages) - 1:
        nav_buttons.append(Button.inline('التالي ➡️', f'admin:users_page:{filter_type}:{page_num + 1}'))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([Button.inline('🔄 تحديث', f'admin:user_stats:{filter_type}'), Button.inline('📊 إحصائيات شاملة', 'admin:detailed_stats')])
    buttons.append([Button.inline('🔙 العودة', 'admin:stats_section')])
    await event.edit(message, buttons=buttons, parse_mode='md')

async def show_detailed_statistics(event):
    """عرض إحصائيات مفصلة"""
    stats = await asyncio.to_thread(db.get_stage_statistics)
    total = stats['total_users']
    stage_1_percent = stats['stage_1'] / total * 100 if total > 0 else 0
    stage_2_percent = stats['stage_2'] / total * 100 if total > 0 else 0
    stage_3_percent = stats['stage_3'] / total * 100 if total > 0 else 0
    stage_4_percent = stats['stage_4'] / total * 100 if total > 0 else 0

    def progress_bar(percent, width=20):
        filled = int(width * percent / 100)
        empty = width - filled
        return '█' * filled + '░' * empty
    message = '📊 **إحصائيات شاملة للبوت**\n'
    message += '━' * 30 + '\n\n'
    message += f"👥 **إجمالي المستخدمين:** {stats['total_users']}\n"
    message += f"📝 **المسجلين في المراحل:** {stats['total_registered']}\n"
    message += f"🆕 **مستخدمين جدد اليوم:** {stats['new_users_today']}\n"
    message += f"✅ **نشط اليوم:** {stats['active_today']}\n\n"
    message += '🎓 **توزيع المراحل:**\n'
    message += f"المرحلة الأولى  : {stats['stage_1']:4d} مستخدم {progress_bar(stage_1_percent)} {stage_1_percent:.1f}%\n"
    message += f"المرحلة الثانية : {stats['stage_2']:4d} مستخدم {progress_bar(stage_2_percent)} {stage_2_percent:.1f}%\n"
    message += f"المرحلة الثالثة : {stats['stage_3']:4d} مستخدم {progress_bar(stage_3_percent)} {stage_3_percent:.1f}%\n"
    message += f"المرحلة الرابعة : {stats['stage_4']:4d} مستخدم {progress_bar(stage_4_percent)} {stage_4_percent:.1f}%\n\n"
    max_stage = max(stats['stage_1'], stats['stage_2'], stats['stage_3'], stats['stage_4'])
    if max_stage > 0:
        message += '📈 **الرسم البياني:**\n'
        scale = 20 / max_stage
        message += f"❶ {'█' * int(stats['stage_1'] * scale)} {stats['stage_1']}\n"
        message += f"❷ {'█' * int(stats['stage_2'] * scale)} {stats['stage_2']}\n"
        message += f"❸ {'█' * int(stats['stage_3'] * scale)} {stats['stage_3']}\n"
        message += f"❹ {'█' * int(stats['stage_4'] * scale)} {stats['stage_4']}\n"
    buttons = [[Button.inline('👥 جميع المستخدمين', 'admin:user_stats:all')], [Button.inline('🎓 المرحلة الأولى', 'admin:user_stats:1'), Button.inline('🎓 المرحلة الثانية', 'admin:user_stats:2')], [Button.inline('🎓 المرحلة الثالثة', 'admin:user_stats:3'), Button.inline('🎓 المرحلة الرابعة', 'admin:user_stats:4')], [Button.inline('🔙 العودة', 'admin:stats_section')]]
    await event.edit(message, buttons=buttons, parse_mode='md')

@client.on(events.CallbackQuery)
async def handle_users_page_callback(event):
    """معالج التنقل بين صفحات المستخدمين"""
    raw = getattr(event, 'data', None)
    try:
        data = raw.decode('utf-8') if raw is not None else ''
    except Exception:
        data = str(raw)
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    if not data.startswith('admin:users_page:'):
        return
    parts = data.split(':')
    if len(parts) < 4:
        return
    filter_type = parts[2]
    page_num = int(parts[3])
    if filter_type == 'all':
        users = await asyncio.to_thread(db.get_users_by_stage)
    else:
        stage = int(filter_type)
        users = await asyncio.to_thread(db.get_users_by_stage, stage)
    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]
    await display_users_page(event, pages, page_num, filter_type)

async def process_add_admin(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    try:
        user_id = int(event.raw_text)
        if user_id == event.sender_id:
            raise ValueError('لا يمكنك إضافة نفسك كأدمن')
        try:
            user = await client.get_entity(user_id)
            username = user.username
            full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        except:
            username = None
            full_name = None
        if await asyncio.to_thread(db.add_admin, user_id=user_id, username=username, full_name=full_name, added_by=event.sender_id):
            await event.reply(f"✅ تمت ترقية المستخدم إلى أدمن بنجاح\n👤 الاسم: {full_name or 'غير معروف'}\n🆔 ID: {user_id}\n📌 اليوزر: @{username}" if username else '📌 بدون يوزرنيم')
        else:
            raise ValueError('فشل في إضافة الأدمن')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def process_add_channel(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    try:
        text = event.raw_text.strip()
        channel_id = None
        channel_username = None
        if text.startswith('https://t.me/'):
            channel_username = text.split('/')[-1]
        elif text.startswith('@'):
            channel_username = text[1:]
        else:
            channel_username = text
        try:
            chat = await client.get_entity(f'@{channel_username}')
            channel_id = str(chat.id)
            channel_title = chat.title
        except Exception:
            raise ValueError('تعذر الحصول على معلومات القناة. تأكد من إضافة البوت كمسؤول في القناة')
        if await asyncio.to_thread(db.add_required_channel, channel_id=channel_id, channel_username=channel_username, channel_title=channel_title, added_by=event.sender_id):
            await event.reply(f'✅ تم إضافة القناة الإلزامية بنجاح\n📌 العنوان: {channel_title}\n👥 اليوزر: @{channel_username}\n🆔 المعرف: {channel_id}')
        else:
            raise ValueError('القناة مضافة مسبقاً')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def process_add_search_channel(event, stage):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    'معالجة إضافة قناة بحث جديدة لمرحلة محددة'
    try:
        text = event.raw_text.strip()
        channel_username = None
        if text.startswith('https://t.me/'):
            channel_username = text.split('/')[-1]
        elif text.startswith('@'):
            channel_username = text[1:]
        else:
            channel_username = text
        try:
            chat = await client.get_entity(f'@{channel_username}')
            channel_id = str(chat.id)
            channel_title = chat.title
        except Exception:
            raise ValueError('تعذر الحصول على معلومات القناة. تأكد من صحة الرابط')
        has_access, status_msg = await ChannelSearch.check_channel_access(channel_username)
        if not has_access:
            await event.reply(f'⚠️ {status_msg}\n\n📌 يرجى إضافة البوت كمسؤول في القناة @{channel_username} أولاً')
            return
        if await asyncio.to_thread(db.add_search_channel, channel_id=channel_id, channel_username=channel_username, channel_title=channel_title, stage=stage, added_by=event.sender_id):
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
            await event.reply(f'✅ تم إضافة قناة البحث بنجاح للمرحلة {stage_name}\n📌 العنوان: {channel_title}\n👥 اليوزر: @{channel_username}\n🆔 المعرف: {channel_id}\n✅ حالة الوصول: {status_msg}')
        else:
            raise ValueError('فشل في إضافة قناة البحث')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def process_add_physics_channel(event):
    # Compatibility for an unfinished workflow from a previous deployment.
    await asyncio.to_thread(clear_pending_action, event.sender_id)
    await event.reply('المعلومات الفيزيائية أصبحت من مكتبة محلية تضم 1000 معلومة؛ لا تحتاج إلى قناة.')

async def process_broadcast_message(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    try:
        users = await asyncio.to_thread(db.get_all_users)
        total = len(users)
        success = 0
        failures = 0
        progress_msg = await event.reply(f'⏳ جارِ إرسال الرسالة إلى {total} مستخدم...')
        for user_id in users:
            if await asyncio.to_thread(db.is_delivery_blocked, user_id):
                failures += 1
                continue
            try:
                if event.text:
                    await safe_send(user_id, event.text)
                elif event.photo:
                    await client.send_file(user_id, event.photo, caption=event.text)
                elif event.video:
                    await client.send_file(user_id, event.video, caption=event.text)
                elif event.document:
                    await client.send_file(user_id, event.document, caption=event.text)
                success += 1
            except Exception as e:
                if isinstance(e, RecipientUnavailableError) or type(e).__name__ in ('UserIsBlockedError', 'InputUserDeactivatedError'):
                    if not isinstance(e, RecipientUnavailableError):
                        await asyncio.to_thread(db.set_delivery_blocked, user_id, True, 'blocked' if type(e).__name__ == 'UserIsBlockedError' else 'deactivated')
                    logging.info('Broadcast skipped: recipient unavailable, user=%s', user_id)
                else:
                    logging.warning('Broadcast failed for user=%s: %s', user_id, type(e).__name__)
                failures += 1
            if (success + failures) % 50 == 0:
                try:
                    await progress_msg.edit(f'⏳ جارِ إرسال الرسالة...\n✅ تم بنجاح: {success}\n❌ فشل: {failures}\n📊 الإجمالي: {total}')
                except:
                    pass
        await progress_msg.edit(f'📊 نتائج الإذاعة:\n✅ تم بنجاح: {success}\n❌ فشل: {failures}\n📊 الإجمالي: {total}')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def handle_support(event, action):
    if action == 'contact':
        user_stage = await asyncio.to_thread(db.get_user_stage, event.sender_id)
        stage = user_stage[0] if user_stage else 4
        await event.reply(f"📩 أرسل رسالتك إلى الدعم (المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]}):")
        await asyncio.to_thread(set_pending_action, event.sender_id, 'support_ticket', {'stage': stage})

async def process_support_ticket(event, stage):
    """معالجة إرسال تذكرة دعم جديدة"""
    try:
        if not event.raw_text:
            await event.reply('❌ يرجى إرسال نص الرسالة')
            return
        ticket_id = await asyncio.to_thread(db.add_support_ticket, event.sender_id, event.raw_text, stage)
        if ticket_id:
            await event.reply(f'✅ تم إرسال تذكرتك بنجاح (#{ticket_id})\nسيتواصل معك الدعم قريباً')
        else:
            await event.reply('❌ فشل في إرسال التذكرة')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def process_ban_user(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    try:
        user_id = int(event.raw_text)
        if await asyncio.to_thread(db.ban_user, user_id):
            await event.reply(f'✅ تم حظر المستخدم {user_id} بنجاح')
        else:
            await event.reply('❌ فشل في حظر المستخدم أو المستخدم غير موجود')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def process_unban_user(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event):
        return
    try:
        user_id = int(event.raw_text)
        if await asyncio.to_thread(db.unban_user, user_id):
            await event.reply(f'✅ تم إلغاء حظر المستخدم {user_id} بنجاح')
        else:
            await event.reply('❌ فشل في إلغاء الحظر أو المستخدم غير موجود')
    except Exception as e:
        await event.reply('تعذر تنفيذ العملية. حاول لاحقاً.')

async def send_daily_physics_info():
    """Send from the same local pool and history as the manual button."""
    eligible = await asyncio.to_thread(db.get_daily_physics_recipients)
    success = failures = 0
    for user_id in eligible:
        try:
            profile = await asyncio.to_thread(db.get_user_stage, user_id)
            if profile and await deliver_physics_fact(user_id, profile[0], daily=True):
                success += 1
        except Exception:
            logging.exception('Daily physics delivery failed for user %s', user_id)
            failures += 1
        await asyncio.sleep(0.05)
    logging.info('Daily local physics facts: sent=%s failed=%s', success, failures)

async def schedule_physics_info():
    """جدولة إرسال المعلومات الفيزيائية يومياً"""
    while True:
        try:
            now = datetime.now(IRAQ_TZ)
            if now.hour >= 9:
                await send_daily_physics_info()
        except Exception:
            logging.exception('Daily physics cycle failed')
        await asyncio.sleep(60)

async def schedule_learning_maintenance():
    from backup_tools import BackupToolError
    next_backup = 0
    while True:
        try:
            for item in await asyncio.to_thread(db.get_due_scheduled_content):
                await asyncio.to_thread(db.queue_schedule,item['id'])
            if time.time() >= next_backup:
                next_backup = time.time() + 3600
                try:
                    backup = await asyncio.to_thread(db.create_backup)
                    destination = STORAGE_CHANNEL_ID or DEVELOPER_ID
                    await asyncio.to_thread(db.enqueue_job,'backup:'+os.path.basename(backup),'backup',{'path':backup,'user_id':destination})
                    next_backup = time.time() + 86400
                except Exception as error:
                    detail = str(error) if isinstance(error, BackupToolError) else 'فشل إعداد النسخة الاحتياطية؛ راجع إعدادات الخدمة والاتصال.'
                    logging.warning('Backup failed; retry in one hour: %s', detail)
                    try: await safe_send(DEVELOPER_ID, '⚠️ فشل النسخ الاحتياطي\n' + detail + '\nستعاد المحاولة بعد ساعة.', parse_mode=None)
                    except Exception: logging.warning('Backup failure alert could not be delivered')
        except Exception:
            logging.exception('Maintenance cycle failed')
        await asyncio.sleep(30)

def worker_handover_timeout():
    value = os.environ.get('WORKER_HANDOVER_TIMEOUT', '300')
    try:
        timeout = float(value)
        if not 30 <= timeout <= 3600:
            raise ValueError()
        return timeout
    except ValueError:
        raise ValueError('WORKER_HANDOVER_TIMEOUT must be between 30 and 3600 seconds') from None

async def wait_for_worker(timeout=300, poll_interval=2):
    """Wait without connecting Telegram while another instance owns the lock."""
    deadline = time.monotonic() + timeout
    next_log = 0
    while True:
        if await asyncio.to_thread(db.claim_worker):
            return
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError('Timed out waiting for the previous bot worker. Check other services using this database.')
        if now >= next_log:
            logging.info('Waiting for the previous bot worker to release its lock')
            next_log = now + 30
        await asyncio.sleep(min(poll_interval, deadline - now))

@client.on(events.NewMessage(pattern='^/(?:deliveries|retry_delivery|admin_stages|blocked_users)(?:@\\w+)?(?:\\s|$)'))
async def operator_delivery_command(event):
    if event.sender_id != DEVELOPER_ID or not await guard_event(event): return
    parts = event.raw_text.split()
    command = parts[0].split('@')[0]
    try:
        if command == '/blocked_users':
            category = parts[1] if len(parts) > 1 else 'blocked'
            page = int(parts[2]) - 1 if len(parts) > 2 else 0
            await show_bot_blocks(event, category, page, edit=False)
        elif command == '/deliveries':
            rows = await asyncio.to_thread(db.unresolved_jobs)
            text = '\n'.join(f'{r[0]} | {r[2]} | {r[3] or ""}' for r in rows)
            await event.reply(text or 'لا توجد إشعارات معلقة.',parse_mode=None)
        elif command == '/retry_delivery' and len(parts)==2:
            ok = await asyncio.to_thread(db.retry_job,parts[1],event.sender_id)
            await event.reply('أعيدت المهمة إلى قائمة الإرسال.' if ok else 'المهمة غير موجودة أو لا تحتاج إعادة محاولة.')
        elif command == '/admin_stages' and len(parts)==3:
            await asyncio.to_thread(db.set_admin_stages,event.sender_id,int(parts[1]),[int(x) for x in parts[2].split(',')])
            await event.reply('تم حفظ مراحل الأدمن.')
        else: await event.reply('/deliveries\n/retry_delivery JOB_ID\n/admin_stages ADMIN_ID 1,2',parse_mode=None)
    except (ValueError,PermissionError): await event.reply('تحقق من المعرف والمراحل.')

async def monitor_worker_lease(owner):
    while True:
        await asyncio.sleep(5)
        if not await asyncio.to_thread(db.worker_lease_alive):
            logging.error('Worker lease lost; stopping Telegram before another worker starts')
            client.runtime_phase='stopping'
            owner.cancel()
            return

async def main():
    tasks = []
    signal_handlers = []
    running_loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    try:
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                running_loop.add_signal_handler(sig, current_task.cancel)
                signal_handlers.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        timeout = worker_handover_timeout()
        client.runtime_phase = 'waiting_worker'
        client.startup_deadline = time.monotonic() + timeout
        # Render must see the HTTP process before it retires the old instance.
        # Telegram remains disconnected until the old worker releases the lock.
        await start_webapp()
        await asyncio.wait_for(asyncio.to_thread(db.initialize_for_worker,timeout),timeout)
        await wait_for_worker(timeout)
        client.runtime_phase = 'starting'
        client.startup_deadline = time.monotonic() + timeout
        print(f'ℹ️ Telethon version: {telethon.__version__}')
        print('⏳ جاري بدء اتصال البوت بعد الحصول على قفل التشغيل...')
        await asyncio.wait_for(client.start(bot_token=BOT_TOKEN), timeout)
        await asyncio.to_thread(db.create_files)
        await asyncio.to_thread(db.insert_default_data)
        await initialize_user_session()
        if MINI_APP_URL:
            try:
                await set_menu_button(BOT_TOKEN, MINI_APP_URL)
            except Exception:
                logging.exception('Mini App menu setup failed')
        from delivery_worker import delivery_loop
        tasks = [asyncio.create_task(monitor_worker_lease(current_task),name='lease'),asyncio.create_task(delivery_loop(),name='delivery'), asyncio.create_task(schedule_physics_info(), name='physics'),
                 asyncio.create_task(schedule_learning_maintenance(), name='maintenance')]
        client.background_tasks = tasks
        client.runtime_phase = 'running'
        print('🟢 البوت جاهز ويستقبل الرسائل...')
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        logging.info('Shutdown requested; disconnecting before releasing the worker lock')
        raise
    except Exception:
        logging.exception('Bot startup or runtime failed')
        raise
    finally:
        client.runtime_phase = 'stopping'
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        client.background_tasks = []
        from webapp import stop_webapp
        try:
            await stop_webapp()
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()
                if user_client and user_client.is_connected():
                    await user_client.disconnect()
            finally:
                # Closing the DB connection/file releases the worker lock last.
                await asyncio.to_thread(db.close)
                for sig in signal_handlers:
                    running_loop.remove_signal_handler(sig)

if __name__ == '__main__':
    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        print('⏹️ تم إيقاف البوت وتحرير قفل التشغيل')
    except KeyboardInterrupt:
        print('\n⏹️ إيقاف البوت...')
    except Exception:
        logging.exception('Fatal startup error')
        sys.exit(1)
    finally:
        try:
            if client.is_connected():
                loop.run_until_complete(client.disconnect())
                print('✅ تم قطع اتصال البوت')
        except Exception as e:
            print('تعذر تنفيذ العملية. حاول لاحقاً.')
        try:
            if user_client and user_client.is_connected():
                loop.run_until_complete(user_client.disconnect())
                print('✅ تم قطع اتصال حساب المساعد')
        except Exception as e:
            print('تعذر تنفيذ العملية. حاول لاحقاً.')
