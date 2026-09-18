import sys
sys.dont_write_bytecode = True
import asyncio
from telethon import events
from styled_buttons import Button
from telethon.errors import MessageNotModifiedError
from config import client, user_client, DEVELOPER_ID, loop, BOT_TOKEN, MINI_APP_URL, STORAGE_CHANNEL_ID
from database import db
from keyboards import Keyboards
from utils import ContentSender, ChannelSearch, check_subscription, send_subscription_message, content_upload_handler
from ai_handler import explain_question
from webapp import set_menu_button, start_webapp
import os
import time
import telethon
from datetime import datetime
from database import IRAQ_TZ

async def initialize_user_session():
    if user_client:
        try:
            if not user_client.is_connected():
                await user_client.start()
                print("✅ جلسة المستخدم للبحث جاهزة")
            else:
                print("✅ جلسة المستخدم للبحث نشطة بالفعل")
            return True
        except Exception as e:
            print(f"❌ فشل في تهيئة جلسة المستخدم: {e}")
            return False
    return True

def get_stage_subjects_count(stage):
    """الحصول على عدد المواد لكل مرحلة"""
    counts = {
        1: {'subjects': 10, 'explanations': 10, 'lab': 3, 'exams': 10},
        2: {'subjects': 12, 'explanations': 12, 'lab': 3, 'exams': 12},
        3: {'subjects': 8, 'explanations': 8, 'lab': 2, 'exams': 8},
        4: {'subjects': 7, 'explanations': 7, 'lab': 3, 'exams': 7, 'research': 3}
    }
    return counts.get(stage, counts[4])


@client.on(events.NewMessage(pattern='/start|/admin'))
async def handle_start(event):
    user_id = event.sender_id

    if db.is_banned(user_id):
        await event.reply("⛔ تم حظرك من استخدام هذا البوت.")
        return

    is_admin = db.is_admin(user_id) or (user_id == DEVELOPER_ID)

    if not await check_subscription(user_id):
        await send_subscription_message(event.chat_id)
        return

    user_stage = db.get_user_stage(user_id)
    if not user_stage:
        await event.reply("👤 مرحباً بك! يرجى إرسال اسمك الثلاثي:")
        set_pending_action(user_id, 'register_name')
        return

    stage = user_stage[0]
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

    welcome_msg = (
        f"أهلاً {user_stage[1]}\n"
        f"• بـوت المـرحلة الـ{stage_name} •\n"
        '• ڪل شيء هـنا لـوجه الله •\n'
        '• لا تـنسونا من دعـائڪم •\n'
    )

    if '/admin' in event.raw_text and not is_admin:
        await event.reply("⛔ ليس لديك صلاحية المسؤول")
        return

    await event.reply(welcome_msg, buttons=Keyboards.main_menu(user_id, stage))

async def process_user_name(event):
    """Process user's full name and show stage selection"""
    user_id = event.sender_id
    full_name = event.raw_text.strip()

    if len(full_name.split()) < 2:
        await event.reply("❌ يرجى إرسال الاسم الثلاثي بشكل صحيح (مثال: عباس غزوان عبد):")
        set_pending_action(user_id, 'register_name')
        return

    user = await event.get_sender()
    db.add_user(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )

    await event.reply(
        f"👤 شكراً {full_name}\nالآن اختر مرحلتك الدراسية:",
        buttons=Keyboards.stage_selection_menu(full_name)
    )

@client.on(events.CallbackQuery)
async def handle_callbacks(event):
    """Handle all callbacks from inline buttons"""
    user_id = event.sender_id
    raw = getattr(event, 'data', None)
    try:
        data = raw.decode('utf-8') if raw is not None else ''
    except Exception:
        data = str(raw)


    if db.is_banned(user_id):
        await event.answer("⛔ تم حظرك من استخدام هذا البوت", alert=True)
        return

    is_admin = db.is_admin(user_id)
    is_developer = user_id == DEVELOPER_ID

    if not await check_subscription(user_id):
        await event.answer("❌ يرجى الاشتراك في القناة أولاً", alert=True)
        await send_subscription_message(event.chat_id)
        return

    parts = data.split(':')
    if is_admin and parts and parts[0] == 'admin':
        db.log_action(user_id, data)

    try:
        if data.startswith('register_stage'):
            await handle_register_stage(event, parts)
        elif data == 'check_subscription':
            await handle_check_subscription(event)
        elif data == 'delete_message':
            await handle_delete_message(event)
        elif parts[0] in ('stage_1','stage_2','stage_3','stage_4','stage_exams','stage_subject','stage_chapter'):
            await handle_stage_callbacks(event, parts, is_admin)
        elif len(parts) > 1 and parts[1] == 'physics_info':
            await handle_physics_info_stage(event, int(parts[0].split('_')[1]))
        elif len(parts) > 1 and parts[1] == 'ai':
            await handle_ai_button_stage(event, int(parts[0].split('_')[1]))
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
        elif parts[0] == 'lab_course':
            await handle_lab_course(event, parts[1], parts[2:])
        elif parts[0] == 'content_comments':
            await handle_content_comments(event, parts[1], parts[2:])
        elif parts[0] == 'content_manage':
            await handle_content_management(event, parts[1], parts[2:], is_admin)
        elif parts[0] == 'learning':
            await handle_learning(event, parts[1], parts[2:], is_admin)
        elif parts[0] == 'none':
            await event.answer("⚠️ لا يوجد محتوى", alert=True)

    except MessageNotModifiedError:
        await event.answer()
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)
        print(f"Callback error: {e}")
        import traceback
        traceback.print_exc()

async def handle_learning(event, action, data, is_admin=False):
    user_id = event.sender_id
    user_stage = db.get_user_stage(user_id)
    stage = user_stage[0] if user_stage else 4
    if action == 'favorite':
        content_id = int(data[0])
        if not db.get_stage_content_by_id(content_id):
            await event.answer("❌ المحتوى غير موجود", alert=True)
            return
        enabled = db.toggle_favorite(user_id, content_id)
        await event.answer("✅ أضيف إلى المفضلة" if enabled else "تمت إزالته من المفضلة", alert=True)
    elif action == 'favorites':
        favorites = db.get_user_favorites(user_id)
        if not favorites:
            await event.edit("⭐ لا يوجد محتوى في المفضلة.", buttons=[[Button.inline("العودة", 'main:home')]])
            return
        buttons = []
        for item in favorites[:30]:
            description = item[8] or item[6] or f'محتوى #{item[10]}'
            buttons.append([Button.inline(f"⭐ {description[:35]}", f'stage_content:view:{item[0]}')])
        buttons.append([Button.inline("العودة", 'main:home')])
        await event.edit(f"⭐ المفضلة ({len(favorites)}):", buttons=buttons)
    elif action == 'progress':
        stats = db.get_user_progress(user_id)
        message = (
            f"📈 تقدمك الدراسي:\n\n"
            f"👁 محتوى تمت مشاهدته: {stats['viewed']}\n"
            f"🔁 مجموع المشاهدات: {stats['total_views']}\n"
            f"🧪 الاختبارات: {stats['quiz_attempts']}\n"
            f"✅ الإجابات الصحيحة: {stats['quiz_correct']} ({stats['quiz_percent']}%)"
        )
        await event.edit(message, buttons=[[Button.inline("🏆 لوحة المتفوقين", 'learning:leaderboard')], [Button.inline("العودة", 'main:home')]])
    elif action == 'notifications':
        if data and data[0] == 'toggle':
            enabled = db.toggle_notifications(user_id)
        else:
            enabled = db.notifications_enabled(user_id)
        status = "مفعلة ✅" if enabled else "معطلة ❌"
        toggle = "❌ تعطيل" if enabled else "✅ تفعيل"
        await event.edit(f"🔔 إشعارات المحتوى: {status}", buttons=[[Button.inline(toggle, 'learning:notifications:toggle')], [Button.inline("العودة", 'main:home')]])
    elif action == 'quiz':
        quiz = db.get_random_quiz(stage)
        if not quiz:
            await event.edit("🧪 لا توجد أسئلة لمرحلتك حاليًا.", buttons=[[Button.inline("العودة", 'main:home')]])
            return
        buttons = [[Button.inline(option, f"learning:answer:{quiz['id']}:{index}")] for index, option in enumerate(quiz['options'])]
        buttons.append([Button.inline("العودة", 'main:home')])
        await event.edit(f"🧪 {quiz['question']}", buttons=buttons)
    elif action == 'answer':
        quiz_id, answer_index = int(data[0]), int(data[1])
        correct = db.record_quiz_attempt(user_id, quiz_id, answer_index)
        quiz = db.get_quiz(quiz_id)
        if correct is None:
            await event.answer("❌ السؤال غير موجود", alert=True)
            return
        result = "✅ إجابة صحيحة" if correct else f"❌ إجابة غير صحيحة\nالصحيح: {quiz['options'][quiz['correct_index']]}"
        await event.edit(result, buttons=[[Button.inline("🧪 سؤال آخر", 'learning:quiz')], [Button.inline("🏆 لوحة المتفوقين", 'learning:leaderboard')], [Button.inline("العودة", 'main:home')]])
    elif action == 'leaderboard':
        rows = db.get_quiz_leaderboard(stage)
        if not rows:
            message = "🏆 لا توجد نتائج بعد."
        else:
            medals = ['🥇', '🥈', '🥉']
            lines = []
            for index, (_, name, correct, attempts) in enumerate(rows):
                rank = medals[index] if index < 3 else f'{index + 1}.'
                lines.append(f"{rank} {name}: {correct}/{attempts}")
            message = "🏆 لوحة المتفوقين:\n\n" + '\n'.join(lines)
        await event.edit(message, buttons=[[Button.inline("العودة", 'main:home')]])
    elif action == 'search':
        set_pending_action(user_id, 'content_search', {'stage': stage})
        await event.reply("🔎 أرسل كلمة أو عبارة للبحث في محتوى مرحلتك:")

async def process_content_search(event, stage):
    query = (event.raw_text or '').strip()
    if len(query) < 2:
        await event.reply("❌ اكتب حرفين على الأقل.")
        return
    results = db.search_content(query, stage)
    if not results:
        await event.reply("🔎 لم أعثر على نتائج.")
        return
    buttons = []
    for content, subject_name in results:
        title = content.get('description') or content.get('file_name') or content.get('content_type')
        buttons.append([Button.inline(f"📚 {subject_name} | {title[:25]}", f"stage_content:view:{content['id']}")])
    await event.reply(f"🔎 نتائج البحث عن: {query}", buttons=buttons)

async def handle_register_stage(event, data):
    """Handle stage selection for new user"""
    try:
        stage = int(data[1])
        full_name = ':'.join(data[2:])

        user_id = event.sender_id

        db.set_user_stage(user_id, full_name, stage)

        if user_id != DEVELOPER_ID:
            try:
                user = await event.get_sender()
            except Exception:
                user = None

            username_text = f"@{user.username}" if (user and getattr(user, 'username', None)) else "بدون يوزرنيم"
            new_user_info = (
                f"👤 مستخدم جديد:\n"
                f"🆔 ID: {user_id}\n"
                f"👤 الاسم: {full_name}\n"
                f"🎓 المرحلة: {stage}\n"
                f"📌 اليوزر: {username_text}"
            )
            try:
                await client.send_message(DEVELOPER_ID, new_user_info)
            except Exception as e:
                print(f"Failed sending new-user notification: {e}")

        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        welcome_msg = (
            f"أهلاً {full_name}\n"
            f"• بـوت المـرحلة الـ{stage_name} •\n"
            '• ڪل شيء هـنا لـوجه الله •\n'
            '• لا تـنسونا من دعـائڪم •\n'
        )

        await event.edit(welcome_msg, buttons=Keyboards.main_menu(user_id, stage))

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_check_subscription(event):
    user_id = event.sender_id

    if await check_subscription(user_id):
        user_stage = db.get_user_stage(user_id)
        if user_stage:
            stage = user_stage[0]
            buttons = Keyboards.main_menu(user_id, stage)
            await event.edit("✅ تم التحقق من الاشتراك بنجاح!", buttons=buttons)
        else:
            await event.edit("✅ تم التحقق من الاشتراك بنجاح! يرجى إكمال التسجيل.")
    else:
        await event.answer("⚠️ لم يتم الاشتراك بعد، يرجى المحاولة مرة أخرى", alert=True)

async def handle_delete_message(event):
    """Delete the current message"""
    try:
        await event.delete()
    except:
        await event.answer("تم الإغلاق")

async def handle_search_page(event, page, data):
    """Handle pagination in search results"""
    try:
        page_num = int(page)

        await event.answer("⏳ جارِ تحميل الصفحة...", alert=True)

        session = search_sessions.get(event.sender_id)
        if not session:
            await event.edit("❌ انتهت جلسة البحث. الرجاء البحث مرة أخرى.", buttons=[[Button.inline("🔍 بحث جديد", f'stage_4:search')]])
            return

        results = session['results']
        query = session['query']
        stage = session['stage']
        page_size = session.get('page_size', 5)

        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        await display_paginated_results_with_links(event, results, query, stage, stage_name, page=page_num, page_size=page_size)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_ai_button_stage(event, stage):
    """معالج زر الذكاء الاصطناعي"""
    try:
        ai_enabled = db.get_ai_enabled()

        if not ai_enabled:
            await event.answer("❌ الذكاء الاصطناعي معطل حالياً من قبل الإدارة", alert=True)
            return

        set_pending_action(event.sender_id, 'ai_question', {'stage': stage})
        await event.reply(
            "🤖 المساعد الذكي:\n\n"
            "أرسل سؤالك الآن وسيتم الرد عليك بواسطة الذكاء الاصطناعي."
        )

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def process_ai_question(event, stage):
    """معالجة سؤال المستخدم للذكاء الاصطناعي"""
    try:
        question = (event.raw_text or '').strip()
        if len(question) < 3:
            await event.reply("❌ يرجى إرسال سؤال أوضح.")
            set_pending_action(event.sender_id, 'ai_question', {'stage': stage})
            return

        progress_msg = await event.reply("🤖 جارِ توليد الإجابة...")
        answer = await explain_question(event.sender_id, question, stage)
        await progress_msg.edit(answer)
    except Exception as e:
        await event.reply(f"❌ خطأ في الذكاء الاصطناعي: {str(e)}")

async def handle_physics_info_stage(event, stage):
    """معالج زر المعلومات الفيزيائية - يعمل عند الطلب والتوزيع التلقائي"""
    try:
        physics_channel = db.get_physics_info_channel()

        if not physics_channel:
            await event.answer("❌ لم يتم تعيين قناة المعلومات الفيزيائية بعد", alert=True)
            return

        user_id = event.sender_id
        channel_username = physics_channel[1]
        messages = []

        if user_client and user_client.is_connected():
            try:
                async for message in user_client.iter_messages(f"@{channel_username}", limit=100):
                    if message.text or message.photo or message.video or message.document:
                        messages.append(message)
            except Exception as uc_error:
                print(f"⚠️ خطأ في جلسة المستخدم: {uc_error}, محاولة جلسة البوت...")
                try:
                    async for message in client.iter_messages(f"@{channel_username}", limit=100):
                        if message.text or message.photo or message.video or message.document:
                            messages.append(message)
                except Exception as bc_error:
                    await event.answer(f"❌ خطأ في جلب المعلومات: {str(bc_error)}", alert=True)
                    return
        else:
            try:
                async for message in client.iter_messages(f"@{channel_username}", limit=100):
                    if message.text or message.photo or message.video or message.document:
                        messages.append(message)
            except Exception as e:
                await event.answer(f"❌ خطأ في جلب المعلومات: {str(e)}", alert=True)
                return

        if not messages:
            await event.answer("❌ لا توجد معلومات متاحة حالياً في القناة", alert=True)
            return

        import random
        random_message = random.choice(messages)

        try:
            if random_message.text:
                await event.reply(f"⚛️ معلومة فيزيائية:\n\n{random_message.text}")
            elif random_message.photo:
                await client.send_file(event.chat_id, random_message.photo, caption="⚛️ معلومة فيزيائية")
            elif random_message.video:
                await client.send_file(event.chat_id, random_message.video, caption="⚛️ معلومة فيزيائية")
            elif random_message.document:
                await client.send_file(event.chat_id, random_message.document, caption="⚛️ معلومة فيزيائية")

            db.add_user_physics_info_request(user_id)
            await event.answer("✅ تم إرسال معلومة فيزيائية لك", alert=True)

        except Exception as send_error:
            await event.answer(f"❌ خطأ في إرسال الرسالة: {str(send_error)}", alert=True)
            print(f"Error sending physics info: {send_error}")

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)
        print(f"Physics info error: {e}")

async def handle_lab_course(event, part1, part2):
    """Handle lab course selection"""
    try:
        if part1 == 'select' and len(part2) >= 2:
            stage = int(part2[0])
            course = int(part2[1])
        else:
            stage = int(part1) if part1.isdigit() else 1
            course = int(part2[0]) if part2 and part2[0].isdigit() else 1

        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        course_name = 'الأول' if course == 1 else 'الثاني'

        await event.edit(
            f"اختر المادة المختبرية - الكورس {course_name} (المرحلة {stage_name}):",
            buttons=Keyboards.stage_category_menu(stage, 'lab', db.is_admin(event.sender_id))
        )
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)
        print(f"Lab course error: {e}")

async def handle_stage_callbacks(event, parts, is_admin):
    """Handle all stage-related callbacks"""
    if parts[0] == 'stage_1' or parts[0] == 'stage_2' or parts[0] == 'stage_3' or parts[0] == 'stage_4':
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
        user_stage = db.get_user_stage(event.sender_id)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        display_name = user_stage[1] if user_stage else "بك"

        welcome_msg = (
            f"أهلاً {display_name}\n"
            f"• بـوت المـرحلة الـ{stage_name} •\n"
            '• ڪل شيء هـنا لـوجه الله •\n'
            '• لا تـنسونا من دعـائڪم •\n'
        )

        await event.edit(
            welcome_msg,
            buttons=Keyboards.main_menu(event.sender_id, stage)
        )
    elif action == 'search':
        await handle_stage_search(event, stage)
    elif action == 'lab':
        await event.edit(
            "اختر كورس المختبر:",
            buttons=Keyboards.lab_courses_menu(stage)
        )
    elif action == 'exams':
        await event.edit(
            "اختر نوع الامتحانات:",
            buttons=Keyboards.exam_type_menu(stage)
        )
    elif action == 'research' and stage == 4:
        await event.edit(
            "قسم البحث - اختر القسم:",
            buttons=Keyboards.stage_category_menu(stage, 'research', db.is_admin(event.sender_id))
        )
    elif action == 'ai':
        await handle_ai_button_stage(event, stage)
    elif action == 'physics_info':
        await handle_physics_info_stage(event, stage)
    else:
        category_map = {
            'subjects': 'subjects',
            'explanations': 'explanations',
            'lab': 'lab',
            'research': 'research'
        }

        if action not in category_map:
            await event.answer("❌ القسم غير موجود", alert=True)
            return

        stage_counts = get_stage_subjects_count(stage)
        category_count = stage_counts.get(category_map[action], 0)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        category_names = {
            'subjects': 'المواد الدراسية',
            'explanations': 'الشروحات',
            'lab': 'المختبر',
            'research': 'قسم البحث',
        }

        await event.edit(
            f"اختر من {category_names[action]} (المرحلة {stage_name}) - العدد: {category_count}:",
            buttons=Keyboards.stage_category_menu(stage, category_map[action], db.is_admin(event.sender_id))
        )

async def handle_exam_type_selection(event, stage, exam_type, is_admin):
    """عرض مواد الامتحانات الشهرية أو النهائية."""
    category_by_type = {
        'monthly': ('monthly_exams', 'الامتحانات الشهرية'),
        'final': ('final_exams', 'الامتحانات النهائية'),
    }
    selected = category_by_type.get(exam_type)
    if not selected:
        await event.answer("❌ نوع الامتحان غير موجود", alert=True)
        return

    category, title = selected
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
    count = len(db.get_stage_subjects_by_category(stage, category))
    await event.edit(
        f"اختر من {title} (المرحلة {stage_name}) - العدد: {count}:",
        buttons=Keyboards.stage_category_menu(stage, category, is_admin)
    )

async def handle_stage_search(event, stage):
    """Handle search functionality for specific stage"""
    await event.reply("🔍 أدخل الكلمة التي تريد البحث عنها:")
    set_pending_action(event.sender_id, 'stage_search', {'stage': stage})

async def process_stage_search_query(event, stage):
    """معالجة استعلام البحث باستخدام جلسة المستخدم"""
    query = event.raw_text.strip()

    if not query or len(query) < 2:
        await event.reply("⚠️ يرجى إدخال كلمة بحث مكونة من حرفين على الأقل")
        return

    search_channels = db.get_search_channels(stage)

    if not search_channels:
        await event.reply("⚠️ لا توجد قنوات بحث مضافة لهذه المرحلة بعد")
        return

    progress_msg = await event.reply(f"🔍 جاري البحث عن: '{query}' في المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]}...")

    try:
        search_results, status_message = await ChannelSearch.search_in_telegram_channels(
            query, search_channels, stage, limit_per_channel=5
        )

        await progress_msg.edit(f"{status_message}\n📊 تم العثور على {len(search_results)} نتيجة")

        await asyncio.sleep(1)

        if not search_results:
            suggestions = await get_search_suggestions(query, stage)
            suggestion_text = "\n💡 اقتراحات بحث: " + ", ".join(suggestions[:3]) if suggestions else ""

            await progress_msg.edit(
                f"❌ لم يتم العثور على نتائج لـ '{query}'\n"
                f"{suggestion_text}\n\n"
                f"🔍 حاول استخدام كلمات بحث أكثر تحديداً"
            )
            return

        search_sessions[event.sender_id] = {
            'query': query,
            'stage': stage,
            'results': search_results,
            'page_size': 5
        }

        await display_search_results_with_links(event, search_results, query, stage, progress_msg)

    except Exception as e:
        await progress_msg.edit(f"❌ حدث خطأ أثناء البحث: {str(e)}")
        print(f"Search error: {e}")

async def display_search_results_with_links(event, results, query, stage, progress_msg):
    """عرض نتائج البحث مع الروابط المباشرة"""
    try:
        await progress_msg.delete()
    except:
        pass

    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

    if len(results) <= 5:
        await display_single_page_results_with_links(event, results, query, stage, stage_name)
    else:
        await display_paginated_results_with_links(event, results, query, stage, stage_name)

async def display_single_page_results_with_links(event, results, query, stage, stage_name):
    """عرض جميع النتائج في صفحة واحدة مع الروابط المباشرة"""
    message = f"**🔍 نتائج البحث عن '{query}' (المرحلة {stage_name}):**\n\n"

    for i, result in enumerate(results, 1):
        message += f"**{i}. {result['channel_title']}**\n"
        message += f"   📝 {result['message_text']}\n"
        message += f"   🔗 [رابط الرسالة المباشر]({result['message_link']})\n"
        message += f"   ⏰ {result['date'].strftime('%Y-%m-%d')}\n"
        message += f"   ⭐ الأهمية: {'★' * min(5, result['relevance']//2)}\n\n"

    buttons = [
        [Button.inline("🔍 بحث جديد", f'stage_{stage}:search')],
        [Button.inline("🏠 القائمة الرئيسية", f'main:home')]
    ]

    await event.reply(message, buttons=buttons, link_preview=False)

async def display_paginated_results_with_links(event, results, query, stage, stage_name, page=0, page_size=5):
    """عرض النتائج على عدة صفحات مع الروابط المباشرة"""
    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_results = results[start_idx:end_idx]

    message = f"**🔍 نتائج البحث عن '{query}' (المرحلة {stage_name})**\n"
    message += f"**الصفحة {page + 1} من {((len(results) - 1) // page_size) + 1}**\n\n"

    for i, result in enumerate(page_results, start_idx + 1):
        message += f"**{i}. {result['channel_title']}**\n"
        message += f"   📝 {result['message_text'][:100]}...\n"
        message += f"   🔗 [رابط الرسالة المباشر]({result['message_link']})\n\n"

    buttons = []

    if page > 0:
        buttons.append(Button.inline("⬅️ السابق", f'search_page:{page-1}'))
    if end_idx < len(results):
        buttons.append(Button.inline("التالي ➡️", f'search_page:{page+1}'))

    if buttons:
        buttons_row = [buttons] if len(buttons) == 1 else [buttons]
        buttons_row.append([Button.inline("🔍 بحث جديد", f'stage_{stage}:search')])
        buttons_row.append([Button.inline("🏠 القائمة الرئيسية", f'main:home')])
    else:
        buttons_row = [
            [Button.inline("🔍 بحث جديد", f'stage_{stage}:search')],
            [Button.inline("🏠 القائمة الرئيسية", f'main:home')]
        ]

    await event.reply(message, buttons=buttons_row, link_preview=False)

async def get_search_suggestions(query, stage):
    """تقديم اقتراحات بحث ذكية"""
    suggestions = []
    stage_keywords = {
        1: ['أولى', 'الصف الأول'],
        2: ['ثانية', 'الصف الثاني'],
        3: ['ثالثة', 'الصف الثالث'],
        4: ['رابعة', 'الصف الرابع']
    }

    stage_words = stage_keywords.get(stage, [])
    for word in stage_words:
        suggestions.append(f"{query} {word}")

    educational_terms = ['شرح', 'امتحان', 'ملخص', 'تمارين', 'حلول']
    for term in educational_terms:
        suggestions.append(f"{term} {query}")
        suggestions.append(f"{query} {term}")

    return suggestions[:5]

async def handle_stage_subject(event, stage, subject_key, is_admin):
    """Handle subject selection for specific stage"""
    subject = db.get_stage_subject(stage, subject_key)
    if not subject:
        await event.answer("❌ المادة غير موجودة", alert=True)
        return

    if stage == 4 and subject_key == 'practical_education':
        content_list = db.get_stage_content(stage, subject_key, 1)
        if content_list:
            await ContentSender.send_stage_content_list(
                event.chat_id, stage, subject_key, 1, content_list, is_admin, show_chapter=False
            )
            await event.answer("📂 تم عرض محتوى التربية العملية")
            return

        buttons = []
        if is_admin:
            buttons.append([Button.inline("➕ إضافة محتوى", f'stage_content:add:{stage}:{subject_key}:1')])
        buttons.append([Button.inline("العودة", f'stage_{stage}:subjects')])
        await event.edit(
            f"▫️ {subject[0]} (المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]})\n\n"
            f"⚠️ لا يوجد محتوى متاح بعد.",
            buttons=buttons
        )
        return

    if subject[3] in ['lab', 'monthly_exams', 'final_exams', 'research']:
        buttons = []
        if is_admin:
            buttons.append([Button.inline("➕ إضافة محتوى", f'stage_content:add:{stage}:{subject_key}:1')])

        if subject[3] == 'monthly_exams':
            back_callback = f'stage_exams:{stage}:monthly'
        elif subject[3] == 'final_exams':
            back_callback = f'stage_exams:{stage}:final'
        else:
            back_callback = f'stage_{stage}:{subject[3]}'
        buttons.append([Button.inline("العودة", back_callback)])

        await event.edit(
            f"▫️ {subject[0]} (المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]})\n\n"
            f"⚠️ لا يوجد محتوى متاح بعد.",
            buttons=buttons
        )
        return

    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
    await event.edit(
        f"اختر الفصل - {subject[0]} (المرحلة {stage_name}):",
        buttons=Keyboards.stage_chapters_menu(stage, subject_key, is_admin)
    )

async def handle_stage_chapter(event, stage, subject_key, chapter_num, is_admin):
    """Handle chapter selection for specific stage"""
    subject = db.get_stage_subject(stage, subject_key)
    if not subject:
        await event.answer("❌ المادة غير موجودة", alert=True)
        return

    content_list = db.get_stage_content(stage, subject_key, chapter_num)

    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

    if content_list:
        await ContentSender.send_stage_content_list(
            event.chat_id, stage, subject_key, chapter_num, content_list, is_admin
        )
        await event.answer("📂 جارِ تحميل قائمة المحتوى...")
    else:
        await event.edit(
            f"▫️ {subject[0]} - الفصل {chapter_num} (المرحلة {stage_name})\n\n"
            f"⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.",
            buttons=Keyboards.stage_chapter_empty_menu(stage, subject_key, chapter_num, is_admin)
        )

async def handle_stage_content(event, action, data, is_admin):
    """Handle stage content management"""
    try:
        if action == 'view':
            content_id = int(data[0])
            await handle_view_content(event, content_id, is_admin)
        elif action == 'add':
            if not is_admin:
                await event.answer("⛔ صلاحية مرفوضة", alert=True)
                return
            stage = int(data[0])
            subject_key = data[1]
            chapter_num = int(data[2])
            await handle_add_content(event, stage, subject_key, chapter_num)
        elif action == 'delete':
            if not is_admin:
                await event.answer("⛔ صلاحية مرفوضة", alert=True)
                return
            content_id = int(data[0])
            await handle_delete_content(event, content_id, is_admin)
        elif action == 'upload_type':
            if not is_admin:
                await event.answer("⛔ صلاحية مرفوضة", alert=True)
                return
            stage = int(data[0])
            subject_key = data[1]
            chapter_num = int(data[2])
            content_type = data[3]
            await handle_upload_type_selection(event, stage, subject_key, chapter_num, content_type)
        elif action == 'cancel_upload':
            await handle_cancel_upload(event)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)
        print(f"Error in handle_stage_content: {e}")

async def handle_upload_type_selection(event, stage, subject_key, chapter_num, content_type):
    """بدء عملية رفع المحتوى بعد اختيار النوع"""
    try:
        subject = db.get_stage_subject(stage, subject_key)
        if not subject:
            await event.answer("❌ المادة غير موجودة", alert=True)
            return

        message = await content_upload_handler.start_upload_session(
            event.sender_id, stage, subject_key, chapter_num, content_type
        )

        buttons = [
            [Button.inline("❌ إلغاء الرفع", f'stage_content:cancel_upload')]
        ]

        await event.edit(message, buttons=buttons)

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_cancel_upload(event):
    """إلغاء عملية الرفع"""
    result = content_upload_handler.cancel_upload(event.sender_id)
    await event.answer(result, alert=True)

    user_stage = db.get_user_stage(event.sender_id)
    stage = user_stage[0] if user_stage else 4
    await event.edit(
        "✅ تم إلغاء عملية الرفع",
        buttons=Keyboards.main_menu(event.sender_id, stage)
    )

async def handle_content_comments(event, action, data):
    """معالج التعليقات على المحتوى"""
    try:
        if action == 'view':
            content_id = int(data[0])
            await handle_view_comments(event, content_id)
        elif action == 'add':
            content_id = int(data[0])
            await event.reply("📝 اكتب تعليقك على هذا المحتوى:")
            set_pending_action(event.sender_id, 'add_comment', {'content_id': content_id})
        elif action == 'delete':
            comment_id = int(data[0])
            content_id = int(data[1]) if len(data) > 1 else None
            await handle_delete_comment(event, comment_id, content_id)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_view_comments(event, content_id):
    """عرض جميع تعليقات المحتوى"""
    try:
        comments = db.get_comments_for_content(content_id)

        if not comments:
            await event.edit("💬 لا توجد تعليقات على هذا المحتوى بعد")
            return

        message = f"💬 التعليقات على هذا المحتوى ({len(comments)}):\n\n"
        buttons = []

        for comment_id, user_id, text, comment_date, first_name, username in comments:
            user_display = f"@{username}" if username else first_name or f"المستخدم {user_id}"
            message += f"👤 {user_display}\n"
            message += f"📝 {text[:100]}{'...' if len(text) > 100 else ''}\n"
            message += f"⏰ {comment_date[:10]}\n\n"

            if event.sender_id == user_id or db.is_admin(event.sender_id):
                buttons.append([Button.inline(f"🗑️ حذف التعليق #{comment_id}", f'content_comments:delete:{comment_id}:{content_id}')])

        buttons.append([Button.inline("➕ إضافة تعليق", f'content_comments:add:{content_id}')])

        await event.edit(message, buttons=buttons)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def process_add_comment(event, content_id):
    """معالجة إضافة تعليق جديد"""
    try:
        if not event.raw_text:
            await event.reply("❌ يرجى إدخال نص التعليق")
            return

        content_data = db.get_stage_content_by_id(content_id)
        if not content_data:
            await event.reply("❌ المحتوى غير موجود")
            return

        stage = content_data[1]

        comment_id = db.add_comment(event.sender_id, content_id, event.raw_text, stage)

        if comment_id:
            await event.reply(f"✅ تم إضافة تعليقك بنجاح (#{comment_id})")
        else:
            await event.reply("⚠️ يرجى الانتظار قبل إضافة تعليق آخر (الحد الأدنى 10 ثوان بين التعليقات)")
    except Exception as e:
        await event.reply(f"❌ خطأ في إضافة التعليق: {str(e)}")

async def handle_delete_comment(event, comment_id, content_id):
    """حذف تعليق معين"""
    try:
        if db.delete_comment(comment_id, event.sender_id):
            await event.answer(f"✅ تم حذف التعليق بنجاح", alert=True)

            if content_id:
                await handle_view_comments(event, content_id)
        else:
            await event.answer("❌ لم تتمكن من حذف هذا التعليق (يجب أن تكون المؤلف أو أدمن)", alert=True)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_view_content(event, content_id, is_admin):
    """عرض محتوى معين"""
    try:
        content_data = db.get_stage_content_by_id(content_id)
        if not content_data:
            await event.answer("❌ المحتوى غير موجود", alert=True)
            return

        sent = await ContentSender.send_stage_single_content(
            event.chat_id,
            content_data,
            is_admin=is_admin
        )
        if sent:
            db.mark_content_viewed(event.sender_id, content_id)
            await event.answer("✅ تم عرض المحتوى")
        else:
            await event.answer("❌ تعذر عرض المحتوى", alert=True)
    except Exception as e:
        await event.answer(f"❌ خطأ في عرض المحتوى: {str(e)}", alert=True)

async def handle_add_content(event, stage, subject_key, chapter_num):
    """بدء عملية إضافة محتوى جديد"""
    try:
        subject = db.get_stage_subject(stage, subject_key)
        if not subject:
            await event.answer("❌ المادة غير موجودة", alert=True)
            return

        await event.edit(
            f"📁 اختر نوع المحتوى الذي تريد إضافته لـ:\n"
            f"📚 {subject[0]} - الفصل {chapter_num}\n\n"
            f"المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]}",
            buttons=Keyboards.content_type_selection_menu(stage, subject_key, chapter_num)
        )
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_delete_content(event, content_id, is_admin):
    """حذف محتوى"""
    try:
        if not is_admin:
            await event.answer("⛔ صلاحية مرفوضة", alert=True)
            return

        content_data = db.get_stage_content_by_id(content_id)
        if not content_data:
            await event.answer("❌ المحتوى غير موجود", alert=True)
            return

        if db.delete_stage_content(content_id):
            await event.answer("✅ تم حذف المحتوى بنجاح", alert=True)

            stage = content_data[1]
            subject_key = content_data[2]
            chapter_num = content_data[3]

            content_list = db.get_stage_content(stage, subject_key, chapter_num)
            if content_list:
                await event.edit(
                    f"📂 محتوى الفصل {chapter_num}:\n"
                    f"✅ تم حذف المحتوى بنجاح",
                    buttons=Keyboards.stage_content_list_menu(stage, subject_key, chapter_num, content_list, is_admin)
                )
            else:
                subject = db.get_stage_subject(stage, subject_key)
                await event.edit(
                    f"▫️ {subject[0]} - الفصل {chapter_num}\n\n"
                    f"✅ تم حذف المحتوى بنجاح\n"
                    f"⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.",
                    buttons=Keyboards.stage_chapter_empty_menu(stage, subject_key, chapter_num, is_admin)
                )
        else:
            await event.answer("❌ فشل في حذف المحتوى", alert=True)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def handle_content_management(event, action, data, is_admin):
    """إدارة المحتوى - معالجة أنواع المحتوى المختلفة"""
    try:
        if not is_admin:
            await event.answer("⛔ صلاحية مرفوضة", alert=True)
            return

        if action == 'type_select':
            stage = int(data[0])
            subject_key = data[1]
            chapter_num = int(data[2])
            content_type = data[3]

            await event.edit(
                f"📝 أرسل الآن {'النص' if content_type == 'text' else 'الملف'} الذي تريد إضافته:\n\n"
                f"📌 سيتم حفظه كـ {content_type}",
                buttons=[[Button.inline("❌ إلغاء", f'stage_chapter:{stage}:{subject_key}:{chapter_num}')]]
            )

            user_id = event.sender_id
            add_content_state[user_id] = {
                'stage': stage,
                'subject_key': subject_key,
                'chapter_num': chapter_num,
                'content_type': content_type,
                'message_id': event.message_id
            }

        elif action == 'skip_description':
            content_id = int(data[0])
            if db.update_content_description(content_id, ""):
                content_data = db.get_stage_content_by_id(content_id)
                if content_data:
                    stage = content_data[1]
                    subject_key = content_data[2]
                    chapter_num = content_data[3]

                    await event.edit(
                        f"✅ تم إضافة المحتوى بنجاح!\n"
                        f"📝 الوصف: بدون وصف"
                    )

                    db.get_stage_subject(stage, subject_key)
                    user = await event.get_sender()
                    added_by_name = f"{user.first_name} {user.last_name}" if user.first_name else f"@{user.username}" if user.username else f"المستخدم {user_id}"

                    success, failures = await ContentSender.notify_new_stage_content(
                        stage, subject_key, chapter_num, content_data[4],
                        "", added_by_name
                    )

                    if success > 0:
                        await event.reply(f"📢 تم إرسال إشعار لـ {success} مستخدم")

                else:
                    await event.reply("✅ تم إضافة المحتوى بنجاح!")
            else:
                await event.reply("❌ فشل في تحديث المحتوى")

            if user_id in add_content_state:
                del add_content_state[user_id]

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

add_content_state = {}

search_sessions = {}

pending_user_actions = {}

def set_pending_action(user_id, action, data=None):
    pending_user_actions[user_id] = {'action': action, 'data': data or {}}

def pop_pending_action(user_id):
    return pending_user_actions.pop(user_id, None)

def clear_pending_action(user_id):
    pending_user_actions.pop(user_id, None)

@client.on(events.NewMessage(pattern=r'^/addquiz(?:\s|$)'))
async def handle_add_quiz_command(event):
    if not db.is_admin(event.sender_id):
        await event.reply("⛔ صلاحية مرفوضة")
        return
    payload = (event.raw_text or '').partition(' ')[2]
    parts = [x.strip() for x in payload.split('|')]
    if len(parts) != 8:
        await event.reply("الصيغة:\n/addquiz المرحلة | subject_key | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الصحيح")
        return
    try:
        stage = int(parts[0])
        correct_index = int(parts[7]) - 1
        if stage not in (1, 2, 3, 4) or correct_index not in range(4):
            raise ValueError
    except ValueError:
        await event.reply("❌ المرحلة من 1 إلى 4، ورقم الإجابة من 1 إلى 4.")
        return
    quiz_id = db.add_quiz(stage, parts[1], parts[2], parts[3:7], correct_index, event.sender_id)
    db.log_action(event.sender_id, 'quiz_added', {'quiz_id': quiz_id})
    await event.reply(f"✅ تمت إضافة السؤال #{quiz_id}")

@client.on(events.NewMessage(pattern=r'^/schedule(?:\s|$)'))
async def handle_schedule_command(event):
    if not db.is_admin(event.sender_id):
        await event.reply("⛔ صلاحية مرفوضة")
        return
    payload = (event.raw_text or '').partition(' ')[2]
    parts = payload.split(maxsplit=1)
    if len(parts) != 2:
        await event.reply("الصيغة:\n/schedule CONTENT_ID YYYY-MM-DD HH:MM")
        return
    try:
        content_id = int(parts[0])
        publish_at = datetime.strptime(parts[1], '%Y-%m-%d %H:%M').replace(tzinfo=IRAQ_TZ)
        if not db.get_stage_content_by_id(content_id):
            raise ValueError
    except ValueError:
        await event.reply("❌ تحقق من معرف المحتوى وصيغة التاريخ.")
        return
    schedule_id = db.schedule_content(content_id, publish_at.isoformat(), event.sender_id)
    db.log_action(event.sender_id, 'content_scheduled', {'schedule_id': schedule_id, 'content_id': content_id})
    await event.reply(f"✅ تمت جدولة المحتوى #{content_id} في {publish_at:%Y-%m-%d %H:%M}")

@client.on(events.NewMessage(pattern=r'^/adminhelp$'))
async def handle_admin_help_command(event):
    if not db.is_admin(event.sender_id):
        return
    await event.reply(
        "🛠 أوامر التطوير:\n\n"
        "/addquiz المرحلة | subject_key | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الصحيح\n"
        "/schedule CONTENT_ID YYYY-MM-DD HH:MM\n"
        "/admin للإحصائيات والنسخ الاحتياطي"
    )

@client.on(events.NewMessage)
async def handle_upload_messages(event):
    """معالجة رسائل المستخدم التي تنتظر خطوة تالية."""
    user_id = event.sender_id

    if event.raw_text and event.raw_text.startswith('/'):
        return

    session = content_upload_handler.get_user_session(user_id)
    if session:
        try:
            if session['step'] == 'description':
                next_message, error = await content_upload_handler.process_description(user_id, event.text)

                if error:
                    await event.reply(error)
                    return

                buttons = [
                    [Button.inline("❌ إلغاء الرفع", f'stage_content:cancel_upload')]
                ]

                await event.reply(next_message, buttons=buttons)

            elif session['step'] == 'content':
                result = await content_upload_handler.process_content(user_id, event)

                await event.reply(result)

                if result.startswith("✅"):
                    stage = session['stage']
                    subject_key = session['subject_key']
                    chapter_num = session['chapter_num']

                    content_list = db.get_stage_content(stage, subject_key, chapter_num)
                    if content_list:
                        await ContentSender.send_stage_content_list(
                            event.chat_id, stage, subject_key, chapter_num, content_list, True
                        )
                    else:
                        subject = db.get_stage_subject(stage, subject_key)
                        await event.reply(
                            f"▫️ {subject[0]} - الفصل {chapter_num}\n\n"
                            f"⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.",
                            buttons=Keyboards.stage_chapter_empty_menu(stage, subject_key, chapter_num, True)
                        )

        except Exception as e:
            await event.reply(f"❌ خطأ في معالجة الرفع: {str(e)}")
            content_upload_handler.cancel_upload(user_id)
        return

    pending = pop_pending_action(user_id)
    if not pending:
        return

    action = pending['action']
    data = pending.get('data', {})

    if action not in ('broadcast',) and (not event.raw_text or event.raw_text.startswith('/')):
        set_pending_action(user_id, action, data)
        return

    try:
        if action == 'register_name':
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
            if (event.raw_text and not event.raw_text.startswith('/')) or event.file:
                await process_broadcast_message(event)
            else:
                set_pending_action(user_id, action, data)
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
        await event.reply(f"❌ خطأ في معالجة الرسالة: {str(e)}")

async def handle_main_menu(event, action, is_admin):
    """Handle main menu callbacks"""
    user_stage = db.get_user_stage(event.sender_id)
    stage = user_stage[0] if user_stage else 4

    if action == 'home':
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        display_name = user_stage[1] if user_stage else "بك"

        welcome_msg = (
            f"أهلاً {display_name}\n"
            f"• بـوت المـرحلة الـ{stage_name} •\n"
            '• ڪل شيء هـنا لـوجه الله •\n'
            '• لا تـنسونا من دعـائڪم •\n'
        )

        await event.edit(
            welcome_msg,
            buttons=Keyboards.main_menu(event.sender_id, stage)
        )
    elif action == 'physics_info':
        await handle_physics_info_stage(event, stage)
    elif action == 'ai':
        await handle_ai_button_stage(event, stage)
    elif action == 'exams':
        await event.edit(
            "اختر نوع الامتحانات:",
            buttons=Keyboards.exam_type_menu(stage)
        )
    else:
        category_map = {
            'subjects': 'subjects',
            'explanations': 'explanations',
            'lab': 'lab',
            'research': 'research'
        }

        if action not in category_map:
            await event.answer("❌ القسم غير موجود", alert=True)
            return

        stage_counts = get_stage_subjects_count(stage)
        category_count = stage_counts.get(category_map[action], 0)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        category_names = {
            'subjects': 'المواد الدراسية',
            'explanations': 'الشروحات',
            'lab': 'المختبر',
            'research': 'قسم البحث'
        }

        await event.edit(
            f"اختر من {category_names[action]} (المرحلة {stage_name}) - العدد: {category_count}:",
            buttons=Keyboards.stage_category_menu(stage, category_map[action], is_admin)
        )

async def handle_support_reply_button(event, ticket_id):
    """معالجة ضغط زر الرد على تذكرة الدعم"""
    try:
        if not (db.is_admin(event.sender_id) or event.sender_id == DEVELOPER_ID):
            await event.answer("⛔ صلاحية مرفوضة", alert=True)
            return

        await event.reply(f"📩 أرسل ردك على تذكرة الدعم #{ticket_id}:")
        set_pending_action(event.sender_id, 'support_reply', {
            'ticket_id': int(ticket_id),
            'original_message_id': event.message_id
        })

    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def process_support_reply(event, ticket_id, original_message_id):
    """معالجة رد الأدمن على تذكرة الدعم"""
    try:
        try:
            ticket_id = int(ticket_id)
        except:
            await event.reply("❌ رقم التذكرة غير صالح")
            return

        if not event.raw_text:
            await event.reply("❌ يرجى إرسال نص الرد")
            return

        ticket_info = db.get_ticket_info(ticket_id)
        if not ticket_info:
            await event.reply("❌ التذكرة غير موجودة")
            return

        user_id = ticket_info[0]
        user_message = ticket_info[1]
        stage = ticket_info[2]

        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        responder = await event.get_sender()
        responder_name = "أدمن الدعم"
        if responder:
            full_name = " ".join(filter(None, [getattr(responder, 'first_name', ''), getattr(responder, 'last_name', '')])).strip()
            responder_name = full_name or (f"@{responder.username}" if getattr(responder, 'username', None) else responder_name)

        reply_msg = (
            f"📩 رد من الدعم على تذكرتك (المرحلة {stage_name}):\n\n"
            f"👤 بواسطة: {responder_name}\n\n"
            f"💬 سؤالك: {user_message}\n\n"
            f"✅ الرد: {event.raw_text}"
        )

        send_error = None
        try:
            await client.send_message(user_id, reply_msg)
        except Exception as e:
            send_error = e

        if send_error and 'user_client' in globals() and user_client:
            try:
                if not user_client.is_connected():
                    await user_client.start()
                await user_client.send_message(user_id, reply_msg)
                send_error = None
            except Exception as e:
                send_error = e

        if send_error:
            await event.reply(f"❌ فشل في إرسال الرد للمستخدم: {send_error}")
            return

        try:
            await client.delete_messages(event.chat_id, [original_message_id])
        except:
            pass

        await event.reply("✅ تم إرسال الرد إلى المستخدم بنجاح")
        db.delete_support_ticket(ticket_id)

    except Exception as e:
        await event.reply(f"❌ خطأ في معالجة الرد: {str(e)}")

async def handle_admin_management(event, action, data_list, is_developer):
    """Handle admin management callbacks"""
    if not is_developer:
        await event.answer("⛔ صلاحية مرفوضة", alert=True)
        return

    if action == 'manage':
        await event.edit(
            "🔐 لوحة إدارة الأدمن:",
            buttons=Keyboards.admin_management_menu()
        )

    elif action == 'admin_section':
        await event.edit(
            "👥 قسم إدارة الأدمنية:",
            buttons=Keyboards.admin_section_menu()
        )

    elif action == 'ban_section':
        await event.edit(
            "🚫 قسم إدارة الحظر:",
            buttons=Keyboards.ban_section_menu()
        )

    elif action == 'stats_section':
        await event.edit(
            "📊 قسم الإحصائيات - اختر التصنيف:",
            buttons=Keyboards.stats_section_menu()
        )

    elif action == 'user_stats':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
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
        stats = db.get_learning_statistics()
        await event.edit(
            "📊 إحصائيات التعلم:\n\n"
            f"⭐ المفضلة: {stats['favorites']}\n"
            f"👁 مشاهدات المحتوى: {stats['content_views']}\n"
            f"👥 طلاب شاهدوا محتوى: {stats['unique_viewers']}\n"
            f"🧪 الأسئلة: {stats['quizzes']}\n"
            f"📝 المحاولات: {stats['quiz_attempts']}\n"
            f"✅ الإجابات الصحيحة: {stats['correct_answers']}",
            buttons=[[Button.inline("العودة", 'admin:manage')]]
        )

    elif action == 'backup':
        backup_file = db.create_backup()
        db.log_action(event.sender_id, 'backup_created', {'file': os.path.basename(backup_file)})
        await client.send_file(event.chat_id, backup_file, caption="💾 نسخة احتياطية لقاعدة البيانات", force_document=True)
        await event.answer("✅ تم إنشاء النسخة الاحتياطية")

    elif action == 'add':
        await event.reply("🔢 أرسل معرف المستخدم (ID) الذي تريد ترقيته إلى أدمن:")
        set_pending_action(event.sender_id, 'add_admin')

    elif action == 'remove':
        admins = db.get_admins()
        if len(admins) <= 1:
            await event.answer("⚠️ لا يوجد أدمن لإزالتهم", alert=True)
            return

        buttons = []
        for admin_id, username, full_name in admins:
            if admin_id != DEVELOPER_ID:
                btn_text = f"➖ {full_name or username or admin_id}"
                buttons.append([Button.inline(btn_text, f'admin:remove_confirm:{admin_id}')])

        buttons.append([Button.inline("العودة", 'admin:admin_section')])

        await event.edit(
            "اختر الأدمن الذي تريد إزالته:",
            buttons=buttons
        )

    elif action == 'remove_confirm':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        admin_id = int(data_list[0])
        admins = db.get_admins()
        target_admin = next((a for a in admins if a[0] == admin_id), None)

        if not target_admin or admin_id == DEVELOPER_ID:
            await event.answer("❌ لا يمكن إزالة هذا الأدمن", alert=True)
            return

        buttons = [
            [Button.inline("✅ تأكيد الإزالة", f'admin:remove_execute:{admin_id}'),
             Button.inline("❌ إلغاء", 'admin:admin_section')]
        ]

        await event.edit(
            f"⚠️ هل أنت متأكد من إزالة الأدمن:\n"
            f"ID: {admin_id}\n"
            f"Username: @{target_admin[1]}\n"
            f"Name: {target_admin[2]}",
            buttons=buttons
        )

    elif action == 'remove_execute':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        admin_id = int(data_list[0])

        if db.remove_admin(admin_id):
            await event.answer("✅ تم إزالة الأدمن بنجاح", alert=True)
            await event.edit(
                "✅ تم إزالة الأدمن بنجاح",
                buttons=Keyboards.admin_section_menu()
            )
        else:
            await event.answer("❌ فشل في إزالة الأدمن", alert=True)

    elif action == 'list':
        admins = db.get_admins()
        message = "👥 قائمة الأدمن:\n\n"

        for admin_id, username, full_name in admins:
            role = " (المطور)" if admin_id == DEVELOPER_ID else ""
            message += f"🔹 {full_name or 'بدون اسم'}\n"
            message += f"   👤 @{username}\n" if username else "   👤 بدون يوزرنيم\n"
            message += f"   🆔 {admin_id}{role}\n\n"

        await event.edit(
            message,
            buttons=Keyboards.admin_section_menu()
        )

    elif action == 'support_admin_stages':
        buttons = [
            [Button.inline("المرحلة الأولى", 'admin:support_admin_select:1'),
             Button.inline("المرحلة الثانية", 'admin:support_admin_select:2')],
            [Button.inline("المرحلة الثالثة", 'admin:support_admin_select:3'),
             Button.inline("المرحلة الرابعة", 'admin:support_admin_select:4')],
            [Button.inline("العودة", 'admin:admin_section')]
        ]
        await event.edit("📩 اختر المرحلة لتعيين أدمن الدعم لها:", buttons=buttons)

    elif action == 'support_admin_select':
        if not data_list:
            await event.answer("❌ لم تُحدد المرحلة", alert=True)
            return
        stage = int(data_list[0])
        admins = [admin for admin in db.get_admins() if admin[0] != DEVELOPER_ID]
        if not admins:
            await event.answer("⚠️ أضف أدمن أولاً", alert=True)
            return
        buttons = [
            [Button.inline(full_name or username or str(admin_id), f'admin:support_admin_set:{stage}:{admin_id}')]
            for admin_id, username, full_name in admins
        ]
        buttons.append([Button.inline("العودة", 'admin:support_admin_stages')])
        await event.edit("📩 اختر أدمن الدعم لهذه المرحلة:", buttons=buttons)

    elif action == 'support_admin_set':
        if len(data_list) < 2:
            await event.answer("❌ بيانات التعيين غير مكتملة", alert=True)
            return
        stage, admin_id = int(data_list[0]), int(data_list[1])
        if db.set_support_admin(stage, admin_id):
            admin = next((item for item in db.get_admins() if item[0] == admin_id), None)
            admin_name = (admin[2] or admin[1] or str(admin_id)) if admin else str(admin_id)
            await event.edit(
                f"✅ تم تعيين {admin_name} لاستلام تذاكر المرحلة {['الأولى', 'الثانية', 'الثالثة', 'الرابعة'][stage - 1]}",
                buttons=Keyboards.admin_section_menu()
            )
        else:
            await event.answer("❌ تعذر تعيين أدمن الدعم", alert=True)

    elif action == 'new_users':
        new_users = db.get_new_users()
        if not new_users:
            await event.answer("⚠️ لا يوجد مستخدمين جدد", alert=True)
            return

        message = "👤 المستخدمين الجدد:\n\n"
        for user_id, first_name, last_name, username in new_users:
            message += f"🔹 {first_name} {last_name}\n" if first_name or last_name else "🔹 مستخدم جديد\n"
            message += f"   👤 @{username}\n" if username else "   👤 بدون يوزرنيم\n"
            message += f"   🆔 {user_id}\n\n"

        await event.reply(message, buttons=Keyboards.stats_section_menu())

    elif action == 'user_stats_old':
        user_count = db.count_users()
        await event.answer(f"👥 عدد المستخدمين: {user_count}", alert=True)

    elif action == 'broadcast':
        await event.reply("📢 أرسل الرسالة التي تريد إذاعتها لجميع المستخدمين:")
        set_pending_action(event.sender_id, 'broadcast')

    elif action == 'channel_manage':
        await event.edit(
            "📌 إدارة قنوات الاشتراك الإجباري:",
            buttons=Keyboards.channel_management_menu()
        )

    elif action == 'channel_add':
        await event.reply("📢 أرسل معرف القناة أو رابطها لإضافتها للاشتراك الإجباري (مثل @shahmplus أو https://t.me/shahmplus):")
        set_pending_action(event.sender_id, 'add_channel')

    elif action == 'channel_remove':
        if not data_list:
            await event.answer("❌ لم يتم تحديد القناة", alert=True)
            return
        record_id = int(data_list[0])
        if db.remove_required_channel(record_id):
            await event.answer("✅ تم إزالة القناة الإلزامية بنجاح", alert=True)
            await event.edit(
                "📌 إدارة قنوات الاشتراك الإجباري:",
                buttons=Keyboards.channel_management_menu()
            )
        else:
            await event.answer("❌ القناة غير موجودة", alert=True)

    elif action == 'search_channels':
        await event.edit(
            "🔍 إدارة قنوات البحث:",
            buttons=Keyboards.search_channels_management()
        )

    elif action == 'add_search_channel':
        await event.edit(
            "🔍 اختر المرحلة لإضافة قناة البحث لها:",
            buttons=Keyboards.stage_selection_for_channel()
        )

    elif action == 'add_search_channel_stage':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        stage = int(data_list[0])
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        await event.reply(f"🔍 أرسل معرف القناة أو رابطها لإضافتها للبحث (المرحلة {stage_name}):")
        set_pending_action(event.sender_id, 'add_search_channel', {'stage': stage})

    elif action == 'remove_search_channel':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        record_id = int(data_list[0])
        stage = int(data_list[1]) if len(data_list) > 1 else None

        channel_info = db.get_search_channel_by_id(record_id)

        if channel_info and db.remove_search_channel(record_id, stage):
            channel_title = channel_info[2]
            channel_username = channel_info[1]
            await event.answer(f"✅ تم إزالة القناة {channel_title} (@{channel_username}) بنجاح", alert=True)
            await event.edit(
                f"✅ تم إزالة القناة {channel_title} (@{channel_username}) بنجاح",
                buttons=Keyboards.search_channels_management()
            )
        else:
            await event.answer("❌ فشل في إزالة القناة أو القناة غير موجودة", alert=True)

    elif action == 'check_all_channels':
        channels = db.get_search_channels()
        message = "**📊 تقرير حالة قنوات البحث:**\n\n"

        for i, channel in enumerate(channels, 1):
            has_access, status_msg = await ChannelSearch.check_channel_access(channel[1])
            status_icon = "✅" if has_access else "❌"
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][channel[3]-1]
            message += f"{i}. {status_icon} {channel[2]} (@{channel[1]}) - المرحلة {stage_name}\n"
            message += f"   📝 {status_msg}\n\n"

        buttons = [[Button.inline("🔄 تحديث التقرير", 'admin:check_all_channels')],
                   [Button.inline("العودة", 'admin:search_channels')]]

        await event.edit(message, buttons=buttons)

    elif action == 'search_stats':
        channels = db.get_search_channels()
        stats_by_stage = {}

        for channel in channels:
            stage = channel[3]
            if stage not in stats_by_stage:
                stats_by_stage[stage] = 0
            stats_by_stage[stage] += 1

        message = "**📊 إحصائيات قنوات البحث:**\n\n"
        for stage in sorted(stats_by_stage.keys()):
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
            message += f"🎓 المرحلة {stage_name}: {stats_by_stage[stage]} قناة\n"

        message += f"\n**الإجمالي: {len(channels)} قناة بحث**"

        await event.answer(message, alert=True)

    elif action == 'select_stage':
        if not data_list:
            await event.edit(
                "🔎 اختر المرحلة التي تريد استعراضها:",
                buttons=Keyboards.admin_stage_selector()
            )
            return

        try:
            stage = int(data_list[0])
        except:
            await event.answer("❌ خطأ في اختيار المرحلة", alert=True)
            return

        await event.edit(
            f"🔎 استعراض المرحلة {['أولى','ثانية','ثالثة','رابعة'][stage-1]}:",
            buttons=Keyboards.main_menu(event.sender_id, stage)
        )

    elif action == 'manage_comments':
        await event.edit(
            "💬 إدارة التعليقات والاستفسارات:",
            buttons=Keyboards.admin_comments_menu()
        )

    elif action == 'ban_user':
        await event.reply("🚫 أرسل معرف المستخدم (ID) الذي تريد حظره:")
        set_pending_action(event.sender_id, 'ban_user')

    elif action == 'unban_user':
        await event.reply("✅ أرسل معرف المستخدم (ID) الذي تريد إلغاء حظره:")
        set_pending_action(event.sender_id, 'unban_user')

    elif action == 'support_tickets':
        await event.edit(
            "📩 تذاكر الدعم المفتوحة:",
            buttons=Keyboards.support_tickets_menu()
        )

    elif action == 'view_ticket':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        ticket_id = int(data_list[0])
        ticket_info = db.get_ticket_info(ticket_id)

        if not ticket_info:
            await event.answer("❌ التذكرة غير موجودة", alert=True)
            return

        message = (
            f"📩 تذكرة الدعم #{ticket_id}\n"
            f"👤 المرسل: {ticket_info[3]} (@{ticket_info[4]})\n"
            f"📅 التاريخ: {ticket_info[5]}\n"
            f"🎓 المرحلة: {['أولى', 'ثانية', 'ثالثة', 'رابعة'][ticket_info[2]-1]}\n\n"
            f"💬 الرسالة:\n{ticket_info[1]}"
        )

        buttons = [
            [Button.inline("📩 الرد على التذكرة", f'support_reply:{ticket_id}')],
            [Button.inline("العودة", 'admin:support_tickets')]
        ]

        await event.reply(message, buttons=buttons)

    elif action == 'close_ticket':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        ticket_id = int(data_list[0])
        if db.delete_support_ticket(ticket_id):
            await event.answer("✅ تم حذف التذكرة", alert=True)
            await event.reply(
                f"✅ تم حذف تذكرة الدعم #{ticket_id}",
                buttons=Keyboards.admin_management_menu()
            )
        else:
            await event.answer("❌ فشل في حذف التذكرة", alert=True)

    elif action == 'ai_settings':
        ai_enabled = db.get_ai_enabled()
        status = "✅ مفعّل" if ai_enabled else "❌ معطّل"

        buttons = [
            [Button.inline("✅ تفعيل الذكاء الاصطناعي", 'admin:ai_toggle:1' if not ai_enabled else 'none'),
             Button.inline("❌ تعطيل الذكاء الاصطناعي", 'admin:ai_toggle:0' if ai_enabled else 'none')],
            [Button.inline("العودة", 'admin:manage')]
        ]

        await event.edit(
            f"🤖 إعدادات الذكاء الاصطناعي:\n\n"
            f"الحالة الحالية: {status}",
            buttons=buttons
        )

    elif action == 'ai_toggle':
        if not data_list:
            await event.answer("❌ خطأ في البيانات", alert=True)
            return

        enabled = int(data_list[0]) == 1
        db.set_ai_enabled(enabled)
        status = "✅ تم تفعيل" if enabled else "✅ تم تعطيل"

        await event.answer(f"{status} الذكاء الاصطناعي", alert=True)

        ai_enabled = db.get_ai_enabled()
        status = "✅ مفعّل" if ai_enabled else "❌ معطّل"

        buttons = [
            [Button.inline("✅ تفعيل الذكاء الاصطناعي", 'admin:ai_toggle:1' if not ai_enabled else 'none'),
             Button.inline("❌ تعطيل الذكاء الاصطناعي", 'admin:ai_toggle:0' if ai_enabled else 'none')],
            [Button.inline("العودة", 'admin:manage')]
        ]

        await event.edit(
            f"🤖 إعدادات الذكاء الاصطناعي:\n\n"
            f"الحالة الحالية: {status}",
            buttons=buttons
        )

    elif action == 'physics_settings':
        physics_channel = db.get_physics_info_channel()

        if physics_channel:
            channel_title = physics_channel[2]
            channel_username = physics_channel[1]

            buttons = [
                [Button.inline("➖ إزالة القناة", 'admin:physics_remove_channel')],
                [Button.url("🔗 الانتقال للقناة", f"https://t.me/{channel_username}")],
                [Button.inline("العودة", 'admin:manage')]
            ]

            await event.edit(
                f"📚 إعدادات المعلومات الفيزيائية:\n\n"
                f"القناة المعيّنة: {channel_title}\n"
                f"اليوزر: @{channel_username}",
                buttons=buttons
            )
        else:
            buttons = [
                [Button.inline("➕ إضافة قناة معلومات", 'admin:physics_add_channel')],
                [Button.inline("العودة", 'admin:manage')]
            ]

            await event.edit(
                f"📚 إعدادات المعلومات الفيزيائية:\n\n"
                f"⚠️ لم يتم تعيين قناة بعد",
                buttons=buttons
            )

    elif action == 'physics_add_channel':
        await event.reply("📚 أرسل معرف قناة المعلومات الفيزيائية أو رابطها (مثل @physics_info):")
        set_pending_action(event.sender_id, 'add_physics_channel')

    elif action == 'physics_remove_channel':
        if db.remove_physics_info_channel():
            await event.answer("✅ تم إزالة قناة المعلومات الفيزيائية بنجاح", alert=True)

            buttons = [
                [Button.inline("➕ إضافة قناة معلومات", 'admin:physics_add_channel')],
                [Button.inline("العودة", 'admin:manage')]
            ]

            await event.edit(
                f"✅ تم إزالة قناة المعلومات الفيزيائية بنجاح\n\n"
                f"📚 إعدادات المعلومات الفيزيائية:\n\n"
                f"⚠️ لم يتم تعيين قناة بعد",
                buttons=buttons
            )
        else:
            await event.answer("❌ فشل في إزالة القناة", alert=True)

async def show_all_users_stats(event):
    """عرض جميع مستخدمين البوت"""
    users = db.get_users_by_stage()
    len(users)

    if not users:
        await event.edit("❌ لا يوجد مستخدمين مسجلين بعد")
        return

    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]

    await display_users_page(event, pages, 0, 'all')

async def show_stage_users_stats(event, stage):
    """عرض مستخدمين مرحلة محددة"""
    stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
    users = db.get_users_by_stage(stage)
    len(users)

    if not users:
        await event.edit(f"❌ لا يوجد طلاب مسجلين في المرحلة {stage_name}")
        return

    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]

    await display_users_page(event, pages, 0, stage)

async def display_users_page(event, pages, page_num, filter_type):
    """عرض صفحة من قائمة المستخدمين"""
    if page_num >= len(pages):
        page_num = 0

    users = pages[page_num]
    total_users = sum(len(p) for p in pages)

    if filter_type == 'all':
        title = "👥 **جميع مستخدمين البوت**"
    else:
        stage = int(filter_type)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        title = f"🎓 **طلاب المرحلة {stage_name}**"

    message = f"{title}\n"
    message += f"📊 **الإجمالي:** {total_users} مستخدم\n"
    message += f"📄 **الصفحة:** {page_num + 1}/{len(pages)}\n"
    message += "━" * 30 + "\n\n"

    for i, user in enumerate(users, page_num * 10 + 1):
        user_id = user['user_id']
        full_name = user['full_name'] or f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        username = user.get('username', '')
        stage_num = user['stage']
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage_num-1]

        date_joined = user.get('date_joined', '')
        if date_joined:
            date_joined = date_joined[:10]

        banned = "🔴 محظور" if user.get('is_banned', False) else "🟢 نشط"

        message += f"**{i}. {full_name}**\n"
        message += f"   🆔 `{user_id}`\n"
        if username:
            message += f"   📌 @{username}\n"
        message += f"   🎓 المرحلة {stage_name}\n"
        message += f"   📅 تاريخ التسجيل: {date_joined}\n"
        message += f"   {banned}\n\n"

    buttons = []

    nav_buttons = []
    if page_num > 0:
        nav_buttons.append(Button.inline("⬅️ السابق", f'admin:users_page:{filter_type}:{page_num-1}'))
    if page_num < len(pages) - 1:
        nav_buttons.append(Button.inline("التالي ➡️", f'admin:users_page:{filter_type}:{page_num+1}'))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([
        Button.inline("🔄 تحديث", f'admin:user_stats:{filter_type}'),
        Button.inline("📊 إحصائيات شاملة", 'admin:detailed_stats')
    ])
    buttons.append([Button.inline("🔙 العودة", 'admin:stats_section')])

    await event.edit(message, buttons=buttons, parse_mode='md')

async def show_detailed_statistics(event):
    """عرض إحصائيات مفصلة"""
    stats = db.get_stage_statistics()

    total = stats['total_users']
    stage_1_percent = (stats['stage_1'] / total * 100) if total > 0 else 0
    stage_2_percent = (stats['stage_2'] / total * 100) if total > 0 else 0
    stage_3_percent = (stats['stage_3'] / total * 100) if total > 0 else 0
    stage_4_percent = (stats['stage_4'] / total * 100) if total > 0 else 0

    def progress_bar(percent, width=20):
        filled = int(width * percent / 100)
        empty = width - filled
        return '█' * filled + '░' * empty

    message = "📊 **إحصائيات شاملة للبوت**\n"
    message += "━" * 30 + "\n\n"

    message += f"👥 **إجمالي المستخدمين:** {stats['total_users']}\n"
    message += f"📝 **المسجلين في المراحل:** {stats['total_registered']}\n"
    message += f"🆕 **مستخدمين جدد اليوم:** {stats['new_users_today']}\n"
    message += f"✅ **نشط اليوم:** {stats['active_today']}\n\n"

    message += "🎓 **توزيع المراحل:**\n"
    message += f"المرحلة الأولى  : {stats['stage_1']:4d} مستخدم {progress_bar(stage_1_percent)} {stage_1_percent:.1f}%\n"
    message += f"المرحلة الثانية : {stats['stage_2']:4d} مستخدم {progress_bar(stage_2_percent)} {stage_2_percent:.1f}%\n"
    message += f"المرحلة الثالثة : {stats['stage_3']:4d} مستخدم {progress_bar(stage_3_percent)} {stage_3_percent:.1f}%\n"
    message += f"المرحلة الرابعة : {stats['stage_4']:4d} مستخدم {progress_bar(stage_4_percent)} {stage_4_percent:.1f}%\n\n"

    max_stage = max(stats['stage_1'], stats['stage_2'], stats['stage_3'], stats['stage_4'])
    if max_stage > 0:
        message += "📈 **الرسم البياني:**\n"
        scale = 20 / max_stage
        message += f"❶ {'█' * int(stats['stage_1'] * scale)} {stats['stage_1']}\n"
        message += f"❷ {'█' * int(stats['stage_2'] * scale)} {stats['stage_2']}\n"
        message += f"❸ {'█' * int(stats['stage_3'] * scale)} {stats['stage_3']}\n"
        message += f"❹ {'█' * int(stats['stage_4'] * scale)} {stats['stage_4']}\n"

    buttons = [
        [Button.inline("👥 جميع المستخدمين", 'admin:user_stats:all')],
        [Button.inline("🎓 المرحلة الأولى", 'admin:user_stats:1'),
         Button.inline("🎓 المرحلة الثانية", 'admin:user_stats:2')],
        [Button.inline("🎓 المرحلة الثالثة", 'admin:user_stats:3'),
         Button.inline("🎓 المرحلة الرابعة", 'admin:user_stats:4')],
        [Button.inline("🔙 العودة", 'admin:stats_section')]
    ]

    await event.edit(message, buttons=buttons, parse_mode='md')

@client.on(events.CallbackQuery)
async def handle_users_page_callback(event):
    """معالج التنقل بين صفحات المستخدمين"""
    raw = getattr(event, 'data', None)
    try:
        data = raw.decode('utf-8') if raw is not None else ''
    except Exception:
        data = str(raw)

    if not data.startswith('admin:users_page:'):
        return

    parts = data.split(':')
    if len(parts) < 4:
        return

    filter_type = parts[2]
    page_num = int(parts[3])

    if filter_type == 'all':
        users = db.get_users_by_stage()
    else:
        stage = int(filter_type)
        users = db.get_users_by_stage(stage)

    page_size = 10
    pages = [users[i:i + page_size] for i in range(0, len(users), page_size)]

    await display_users_page(event, pages, page_num, filter_type)

async def process_add_admin(event):
    try:
        user_id = int(event.raw_text)
        if user_id == event.sender_id:
            raise ValueError("لا يمكنك إضافة نفسك كأدمن")

        try:
            user = await client.get_entity(user_id)
            username = user.username
            full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        except:
            username = None
            full_name = None

        if db.add_admin(
            user_id=user_id,
            username=username,
            full_name=full_name,
            added_by=event.sender_id
        ):
            await event.reply(
                f"✅ تمت ترقية المستخدم إلى أدمن بنجاح\n"
                f"👤 الاسم: {full_name or 'غير معروف'}\n"
                f"🆔 ID: {user_id}\n"
                f"📌 اليوزر: @{username}" if username else "📌 بدون يوزرنيم"
            )
        else:
            raise ValueError("فشل في إضافة الأدمن")

    except Exception as e:
        await event.reply(f"❌ خطأ في إضافة الأدمن: {str(e)}")

async def process_add_channel(event):
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
            chat = await client.get_entity(f"@{channel_username}")
            channel_id = str(chat.id)
            channel_title = chat.title
        except Exception:
            raise ValueError("تعذر الحصول على معلومات القناة. تأكد من إضافة البوت كمسؤول في القناة")

        if db.add_required_channel(
            channel_id=channel_id,
            channel_username=channel_username,
            channel_title=channel_title,
            added_by=event.sender_id
        ):
            await event.reply(
                f"✅ تم إضافة القناة الإلزامية بنجاح\n"
                f"📌 العنوان: {channel_title}\n"
                f"👥 اليوزر: @{channel_username}\n"
                f"🆔 المعرف: {channel_id}"
            )
        else:
            raise ValueError("القناة مضافة مسبقاً")

    except Exception as e:
        await event.reply(f"❌ خطأ في إضافة القناة: {str(e)}")

async def process_add_search_channel(event, stage):
    """معالجة إضافة قناة بحث جديدة لمرحلة محددة"""
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
            chat = await client.get_entity(f"@{channel_username}")
            channel_id = str(chat.id)
            channel_title = chat.title
        except Exception:
            raise ValueError("تعذر الحصول على معلومات القناة. تأكد من صحة الرابط")

        has_access, status_msg = await ChannelSearch.check_channel_access(channel_username)

        if not has_access:
            await event.reply(
                f"⚠️ {status_msg}\n\n"
                f"📌 يرجى إضافة البوت كمسؤول في القناة @{channel_username} أولاً"
            )
            return

        if db.add_search_channel(
            channel_id=channel_id,
            channel_username=channel_username,
            channel_title=channel_title,
            stage=stage,
            added_by=event.sender_id
        ):
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
            await event.reply(
                f"✅ تم إضافة قناة البحث بنجاح للمرحلة {stage_name}\n"
                f"📌 العنوان: {channel_title}\n"
                f"👥 اليوزر: @{channel_username}\n"
                f"🆔 المعرف: {channel_id}\n"
                f"✅ حالة الوصول: {status_msg}"
            )
        else:
            raise ValueError("فشل في إضافة قناة البحث")

    except Exception as e:
        await event.reply(f"❌ خطأ في إضافة قناة البحث: {str(e)}")

async def process_add_physics_channel(event):
    """معالجة إضافة قناة المعلومات الفيزيائية"""
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
            chat = await client.get_entity(f"@{channel_username}")
            channel_id = str(chat.id)
            channel_title = chat.title
        except Exception:
            raise ValueError("تعذر الحصول على معلومات القناة. تأكد من صحة الرابط")

        if db.set_physics_info_channel(
            channel_id=channel_id,
            channel_username=channel_username,
            channel_title=channel_title,
            added_by=event.sender_id
        ):
            await event.reply(
                f"✅ تم تعيين قناة المعلومات الفيزيائية بنجاح\n"
                f"📌 العنوان: {channel_title}\n"
                f"👥 اليوزر: @{channel_username}\n"
                f"🆔 المعرف: {channel_id}"
            )
        else:
            raise ValueError("فشل في تعيين القناة")

    except Exception as e:
        await event.reply(f"❌ خطأ في إضافة القناة: {str(e)}")

async def process_broadcast_message(event):
    try:
        users = db.get_all_users()
        total = len(users)
        success = 0
        failures = 0

        progress_msg = await event.reply(f"⏳ جارِ إرسال الرسالة إلى {total} مستخدم...")

        for user_id in users:
            try:
                if event.text:
                    await client.send_message(user_id, event.text)
                elif event.photo:
                    await client.send_file(user_id, event.photo, caption=event.text)
                elif event.video:
                    await client.send_file(user_id, event.video, caption=event.text)
                elif event.document:
                    await client.send_file(user_id, event.document, caption=event.text)
                success += 1
            except Exception as e:
                print(f"Failed to send broadcast to {user_id}: {e}")
                failures += 1

            if (success + failures) % 50 == 0:
                try:
                    await progress_msg.edit(
                        f"⏳ جارِ إرسال الرسالة...\n"
                        f"✅ تم بنجاح: {success}\n"
                        f"❌ فشل: {failures}\n"
                        f"📊 الإجمالي: {total}"
                    )
                except:
                    pass

        await progress_msg.edit(
            f"📊 نتائج الإذاعة:\n"
            f"✅ تم بنجاح: {success}\n"
            f"❌ فشل: {failures}\n"
            f"📊 الإجمالي: {total}"
        )

    except Exception as e:
        await event.reply(f"❌ خطأ في الإذاعة: {str(e)}")

async def handle_support(event, action):
    if action == 'contact':
        user_stage = db.get_user_stage(event.sender_id)
        stage = user_stage[0] if user_stage else 4

        await event.reply(f"📩 أرسل رسالتك إلى الدعم (المرحلة {['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]}):")
        set_pending_action(event.sender_id, 'support_ticket', {'stage': stage})

async def process_support_ticket(event, stage):
    """معالجة إرسال تذكرة دعم جديدة"""
    try:
        if not event.raw_text:
            await event.reply("❌ يرجى إرسال نص الرسالة")
            return

        ticket_id = db.add_support_ticket(event.sender_id, event.raw_text, stage)

        if ticket_id:
            await event.reply(f"✅ تم إرسال تذكرتك بنجاح (#{ticket_id})\nسيتواصل معك الدعم قريباً")

            user = await event.get_sender()
            user_info = f"{user.first_name} (@{user.username})" if user.username else user.first_name
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

            notify_msg = (
                f"📩 تذكرة دعم جديدة #{ticket_id} (المرحلة {stage_name}):\n"
                f"👤 المستخدم: {user_info}\n"
                f"🆔 ID: {event.sender_id}\n\n"
                f"💬 الرسالة: {event.raw_text}"
            )

            recipient_ids = {DEVELOPER_ID}
            stage_support_admin = db.get_support_admin(stage)
            if stage_support_admin:
                recipient_ids.add(stage_support_admin)
            sent_count = 0
            for admin_id in recipient_ids:
                try:
                    buttons = [[Button.inline("📩 الرد على التذكرة", f'support_reply:{ticket_id}')]]
                    await client.send_message(admin_id, notify_msg, buttons=buttons)
                    sent_count += 1
                except Exception as e:
                    print(f"Failed to send notification to admin {admin_id}: {e}")

        else:
            await event.reply("❌ فشل في إرسال التذكرة")

    except Exception as e:
        await event.reply(f"❌ خطأ في إرسال التذكرة: {str(e)}")

async def process_ban_user(event):
    try:
        user_id = int(event.raw_text)
        if db.ban_user(user_id):
            await event.reply(f"✅ تم حظر المستخدم {user_id} بنجاح")
        else:
            await event.reply("❌ فشل في حظر المستخدم أو المستخدم غير موجود")
    except Exception as e:
        await event.reply(f"❌ خطأ: {str(e)}")

async def process_unban_user(event):
    try:
        user_id = int(event.raw_text)
        if db.unban_user(user_id):
            await event.reply(f"✅ تم إلغاء حظر المستخدم {user_id} بنجاح")
        else:
            await event.reply("❌ فشل في إلغاء الحظر أو المستخدم غير موجود")
    except Exception as e:
        await event.reply(f"❌ خطأ: {str(e)}")

async def send_daily_physics_info():
    """إرسال معلومة فيزيائية يومية للمستخدمين (تلقائياً)"""
    import random

    physics_channel = db.get_physics_info_channel()
    if not physics_channel:
        print("⚠️ قناة المعلومات الفيزيائية لم تُعيّن بعد")
        return

    try:
        channel_username = physics_channel[1]

        messages = []

        if user_client and user_client.is_connected():
            try:
                async for message in user_client.iter_messages(f"@{channel_username}", limit=100):
                    if message.text or message.photo or message.video or message.document:
                        messages.append(message)
            except Exception as e:
                print(f"⚠️ خطأ جلسة المستخدم: {e}, محاولة جلسة البوت...")
                try:
                    async for message in client.iter_messages(f"@{channel_username}", limit=100):
                        if message.text or message.photo or message.video or message.document:
                            messages.append(message)
                except Exception as e2:
                    print(f"❌ خطأ جلسة البوت: {e2}")
                    return
        else:
            try:
                async for message in client.iter_messages(f"@{channel_username}", limit=100):
                    if message.text or message.photo or message.video or message.document:
                        messages.append(message)
            except Exception as e:
                print(f"❌ خطأ في جلب الرسائل: {e}")
                return

        if not messages:
            print("⚠️ لا توجد رسائل في قناة المعلومات الفيزيائية")
            return

        random_message = random.choice(messages)

        eligible_users = db.get_users_eligible_for_physics_info()

        print(f"🚀 جاري إرسال معلومة فيزيائية إلى {len(eligible_users)} مستخدم مؤهل...")

        success = 0
        failures = 0

        for user_id in eligible_users:
            try:
                if random_message.text:
                    await client.send_message(user_id, f"⚛️ معلومة فيزيائية يومية:\n\n{random_message.text}")
                elif random_message.photo:
                    await client.send_file(user_id, random_message.photo, caption="⚛️ معلومة فيزيائية يومية")
                elif random_message.video:
                    await client.send_file(user_id, random_message.video, caption="⚛️ معلومة فيزيائية يومية")
                elif random_message.document:
                    await client.send_file(user_id, random_message.document, caption="⚛️ معلومة فيزيائية يومية")

                db.add_user_physics_info_request(user_id)
                success += 1

            except Exception as e:
                print(f"⚠️ فشل إرسال لـ {user_id}: {e}")
                failures += 1

        print(f"✅ تم إرسال معلومة فيزيائية إلى {success} مستخدم (فشل: {failures})")

    except Exception as e:
        print(f"❌ خطأ في إرسال المعلومات الفيزيائية: {e}")
        import traceback
        traceback.print_exc()

async def schedule_physics_info():
    """جدولة إرسال المعلومات الفيزيائية يومياً"""
    import asyncio
    from datetime import datetime, timedelta

    while True:
        try:
            now = datetime.now()
            next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)

            if now > next_run:
                next_run += timedelta(days=1)

            delay = (next_run - now).total_seconds()
            print(f"⏰ سيتم إرسال المعلومات الفيزيائية في {next_run.strftime('%Y-%m-%d %H:%M:%S')}")

            await asyncio.sleep(delay)

            await send_daily_physics_info()

        except Exception as e:
            print(f"❌ خطأ في جدولة المعلومات الفيزيائية: {e}")
            await asyncio.sleep(3600)

async def schedule_learning_maintenance():
    last_backup = 0
    while True:
        try:
            for item in db.get_due_scheduled_content():
                content = db.get_stage_content_by_id(item['content_id'])
                if not content:
                    db.mark_schedule_sent(item['id'])
                    continue
                stage = content[1]
                subject = db.get_stage_subject(stage, content[2])
                subject_name = subject[0] if subject else content[2]
                message = f"📢 محتوى مجدول جديد\n📚 {subject_name}\n📝 {content[8] or 'بدون وصف'}"
                for user_id in db.get_all_users():
                    user_stage = db.get_user_stage(user_id)
                    if not user_stage or user_stage[0] != stage or not db.notifications_enabled(user_id):
                        continue
                    try:
                        await client.send_message(user_id, message, buttons=[[Button.inline("👁 عرض المحتوى", f"stage_content:view:{content[0]}")]])
                    except Exception:
                        pass
                db.mark_schedule_sent(item['id'])
            now = time.time()
            if now - last_backup >= 86400:
                backup_file = db.create_backup()
                if STORAGE_CHANNEL_ID:
                    await client.send_file(STORAGE_CHANNEL_ID, backup_file, caption="💾 نسخة احتياطية يومية لقاعدة البيانات", force_document=True)
                last_backup = now
        except Exception as e:
            print(f"❌ خطأ في صيانة نظام التعلم: {e}")
        await asyncio.sleep(30)

async def main():
    try:
        print(f"ℹ️ Telethon version: {telethon.__version__}")
        print("⏳ جاري بدء اتصال البوت...")
        await client.start(bot_token=BOT_TOKEN)
        print("✅ البوت متصل بنجاح!")

        try:
            db.create_files()
            db.insert_default_data()
        except Exception:
            pass

        await initialize_user_session()

        if MINI_APP_URL:
            await start_webapp()
            print("✅ تم تشغيل Mini App")
            try:
                await set_menu_button(BOT_TOKEN, MINI_APP_URL)
                print("✅ تم تفعيل زر Open")
            except Exception as e:
                print(f"⚠️ تعذر تفعيل زر Open: {e}")

        asyncio.create_task(schedule_physics_info())
        asyncio.create_task(schedule_learning_maintenance())
        print("✅ تم تفعيل جدولة المعلومات الفيزيائية")

        print("🟢 البوت جاهز ويستقبل الرسائل...")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"❌ خطأ في تشغيل البوت: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n⏹️ إيقاف البوت...")
    except Exception as e:
        print(f"❌ خطأ غير متوقع: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if client.is_connected():
                loop.run_until_complete(client.disconnect())
                print("✅ تم قطع اتصال البوت")
        except Exception as e:
            print(f"⚠️ خطأ في قطع اتصال البوت: {e}")

        try:
            if user_client and user_client.is_connected():
                loop.run_until_complete(user_client.disconnect())
                print("✅ تم قطع اتصال حساب المساعد")
        except Exception as e:
            print(f"⚠️ خطأ في قطع اتصال حساب المساعد: {e}")
