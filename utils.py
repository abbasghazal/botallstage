import asyncio
import os
import time
import logging
from telethon import utils as telethon_utils
from styled_buttons import Button
from telethon.errors import ChannelInvalidError, ChannelPrivateError
from telethon.errors import FloodWaitError
from config import client, user_client, DEVELOPER_ID, STORAGE_CHANNEL_ID
from database import db
try:
    MAX_UPLOAD_MB = max(1, min(2000, int(os.environ.get('MAX_UPLOAD_MB', '200'))))
except ValueError:
    raise RuntimeError('Invalid MAX_UPLOAD_MB')
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

class ContentSender:

    @staticmethod
    def _category_back_callback(stage, category):
        if category == 'monthly_exams':
            return f'stage_exams:{stage}:monthly'
        if category == 'final_exams':
            return f'stage_exams:{stage}:final'
        return f'stage_{stage}:{category}'

    @staticmethod
    async def send_stage_single_content(chat_id, content_data, is_admin=False):
        """Send specific content item with admin options"""
        content_id, stage, subject_key, chapter_num, content_type, file_id, file_name, text, description, added_by, content_number = content_data
        try:
            subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
            subject_name = subject[0] if subject else 'مادة غير معروفة'
            caption = f'📝 الوصف: {description[:500]}\n\n' if description else ''
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
            section = await asyncio.to_thread(db.chapter_label, stage, subject_key, chapter_num)
            full_caption = f'{caption}📚 {subject_name} | {section} | 🎓 المرحلة {stage_name} | 🔢 #{content_number}'
            if content_type != 'text':
                media_source = file_id
                storage_message_id = await asyncio.to_thread(db.get_content_storage_message_id, content_id)
                if STORAGE_CHANNEL_ID and storage_message_id:
                    stored_message = await client.get_messages(STORAGE_CHANNEL_ID, ids=storage_message_id)
                    if not stored_message or not stored_message.media:
                        raise ValueError('ملف قناة التخزين غير موجود')
                    media_source = stored_message.media
                elif not isinstance(file_id, str) or telethon_utils.resolve_bot_file_id(file_id) is None:
                    raise ValueError('المحتوى لا يحتوي على معرف ملف Telegram صالح')
            if content_type == 'video':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'document':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'photo':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'text' and text:
                body = f'{full_caption}\n\n{text}'
                for offset in range(0, len(body), 3500):
                    await client.send_message(chat_id, body[offset:offset + 3500], parse_mode=None)
            elif content_type == 'audio':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'voice':
                await client.send_file(chat_id, media_source, caption=full_caption.strip(), voice_note=True)
            else:
                await client.send_message(chat_id, 'نوع المحتوى غير معروف.')
                return False
            if is_admin:
                buttons = [[Button.inline('🗑️ حذف هذا المحتوى', f'stage_content:delete:{content_id}')], [Button.inline('العودة للفصل', f'stage_chapter:{stage}:{subject_key}:{chapter_num}')]]
                await client.send_message(chat_id, 'خيارات الأدمن:', buttons=buttons)
            else:
                favorite_text = '⭐ إزالة من المفضلة' if await asyncio.to_thread(db.is_favorite, chat_id, content_id) else '⭐ إضافة للمفضلة'
                buttons = [[Button.inline(favorite_text, f'learning:favorite:{content_id}')], [Button.inline('💬 عرض التعليقات', f'content_comments:view:{content_id}')], [Button.inline('➕ إضافة تعليق', f'content_comments:add:{content_id}')], [Button.inline('العودة', f'stage_chapter:{stage}:{subject_key}:{chapter_num}')]]
                await client.send_message(chat_id, 'خيارات المحتوى:', buttons=buttons)
            return True
        except Exception as e:
            print(f'Error sending content: {e}')
            await client.send_message(chat_id, 'تعذر تنفيذ العملية. حاول لاحقاً.')
            return False

    @staticmethod
    async def send_stage_content_list(chat_id, stage, subject_key, chapter_num, content_list, is_admin=False, show_chapter=True, page=0):
        """إرسال قائمة محتوى الفصل"""
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        subject_name = subject[0] if subject else 'مادة غير معروفة'
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        section = await asyncio.to_thread(db.chapter_label, stage, subject_key, chapter_num)
        chapter_label = ' - ' + section if show_chapter else ''
        message = f'📂 محتوى {subject_name}{chapter_label} (المرحلة {stage_name}):\n\n'
        if not content_list:
            message += '⚠️ لا يوجد محتوى متاح لهذا الفصل بعد.'
            await client.send_message(chat_id, message)
            return
        total = len(content_list)
        page = max(0, min(int(page), max(0, (total - 1) // 20)))
        content_list = content_list[page * 20:(page + 1) * 20]
        for i, (content_id, content_type, description, date_added, content_number) in enumerate(content_list, 1):
            icon = '🎥' if content_type == 'video' else '📄' if content_type == 'document' else '🖼️' if content_type == 'photo' else '📝' if content_type == 'text' else '🎵' if content_type == 'audio' else '🎤'
            desc_display = description[:30] + '...' if description and len(description) > 30 else description or f'محتوى #{content_number}'
            message += f'{i}. {icon} {desc_display} (#{content_number})\n'
        buttons = []
        for content_id, content_type, description, date_added, content_number in content_list:
            icon = '🎥' if content_type == 'video' else '📄' if content_type == 'document' else '🖼️' if content_type == 'photo' else '📝' if content_type == 'text' else '🎵' if content_type == 'audio' else '🎤'
            btn_text = f'{icon} #{content_number} - {description[:20]}...' if description else f'{icon} محتوى #{content_number}'
            buttons.append([Button.inline(btn_text, f'stage_content:view:{content_id}')])
        pagination = []
        if page > 0:
            pagination.append(Button.inline('السابق', f'content_page:{stage}:{subject_key}:{chapter_num}:{page - 1}'))
        if (page + 1) * 20 < total:
            pagination.append(Button.inline('التالي', f'content_page:{stage}:{subject_key}:{chapter_num}:{page + 1}'))
        if pagination:
            buttons.append(pagination)
        if is_admin:
            buttons.append([Button.inline('➕ إضافة محتوى', f'stage_content:add:{stage}:{subject_key}:{chapter_num}')])
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        buttons.append([Button.inline('العودة', ContentSender._category_back_callback(stage, subject[3]))])
        await client.send_message(chat_id, message, buttons=buttons)

    @staticmethod
    async def notify_new_stage_content(stage, subject_key, chapter_num, content_type, description, added_by_name):
        """Notify all users about new content in specific stage"""
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        if not subject:
            return (False, 0)
        content_type_names = {'video': 'فيديو', 'document': 'ملف', 'photo': 'صورة', 'text': 'نص', 'audio': 'ملف صوتي', 'voice': 'بصمة صوتية'}
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        message = f"📢 إشعار جديد (المرحلة {stage_name}):\nقام الأدمن {added_by_name} برفع {content_type_names.get(content_type, 'محتوى')} جديد\n📚 لـ {subject[0]} - الفصل {chapter_num}\n📝 الوصف: {(description if description else 'لا يوجد وصف')}\n\n🔍 استعرض المحتوى من خلال البوت"
        users = await asyncio.to_thread(db.get_all_users)
        success = 0
        failures = 0
        batch_size = 20
        delay_between_batches = 1.0
        batch = []
        for uid in users:
            user_stage_data = await asyncio.to_thread(db.get_user_stage, uid)
            if not user_stage_data or user_stage_data[0] != stage:
                continue
            if not await asyncio.to_thread(db.notifications_enabled, uid):
                continue
            batch.append(uid)
            if len(batch) >= batch_size:
                for user_id in batch:
                    try:
                        await client.send_message(user_id, message)
                        success += 1
                    except FloodWaitError as fe:
                        wait = getattr(fe, 'seconds', None) or 5
                        print(f'FloodWait: sleeping {wait}s')
                        await asyncio.sleep(wait)
                        try:
                            await client.send_message(user_id, message)
                            success += 1
                        except Exception as e:
                            print(f'Failed after floodwait to {user_id}: {e}')
                            failures += 1
                    except Exception as e:
                        print(f'Failed to send notification to {user_id}: {e}')
                        failures += 1
                batch = []
                await asyncio.sleep(delay_between_batches)
        if batch:
            for user_id in batch:
                try:
                    await client.send_message(user_id, message)
                    success += 1
                except FloodWaitError as fe:
                    wait = getattr(fe, 'seconds', None) or 5
                    print(f'FloodWait: sleeping {wait}s')
                    await asyncio.sleep(wait)
                    try:
                        await client.send_message(user_id, message)
                        success += 1
                    except Exception as e:
                        print(f'Failed after floodwait to {user_id}: {e}')
                        failures += 1
                except Exception as e:
                    print(f'Failed to send notification to {user_id}: {e}')
                    failures += 1
        return (success, failures)

class ContentUploadHandler:
    """معالج رفع المحتوى الجديد"""

    def __init__(self):
        from workflows import WorkflowMap
        self.upload_sessions = WorkflowMap('upload')

    async def start_upload_session(self, user_id, stage, subject_key, chapter_num, content_type):
        """بدء جلسة رفع محتوى جديدة"""
        if not await asyncio.to_thread(db.is_admin, user_id) or await asyncio.to_thread(db.is_banned, user_id):
            raise PermissionError('Upload forbidden')
        if stage not in (1, 2, 3, 4) or not await asyncio.to_thread(db.is_valid_chapter, stage, subject_key, chapter_num) or (not await asyncio.to_thread(db.get_stage_subject, stage, subject_key)):
            raise ValueError('Invalid upload target')
        if not await asyncio.to_thread(db.can_manage_stage,user_id,stage): raise PermissionError('Wrong admin stage')
        await asyncio.to_thread(self.upload_sessions.__setitem__,user_id,{'at': time.time(), 'stage': stage, 'subject_key': subject_key, 'chapter_num': chapter_num, 'content_type': content_type, 'step': 'description'})
        subject = await asyncio.to_thread(db.get_stage_subject, stage, subject_key)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage - 1]
        content_type_names = {'video': 'فيديو', 'document': 'ملف', 'photo': 'صورة', 'text': 'نص', 'audio': 'ملف صوتي', 'voice': 'بصمة صوتية'}
        label = await asyncio.to_thread(db.chapter_label, stage, subject_key, chapter_num)
        message = f"⬆️ بدء رفع {content_type_names[content_type]}:\n📚 المادة: {subject[0]}\n🎓 المرحلة: {stage_name}\n📖 {label}\n\n📝 الرجاء إرسال الوصف أولاً (أو اكتب 'بدون' لعدم إضافة وصف):"
        return message

    async def process_description(self, user_id, description_text):
        """معالجة وصف المحتوى"""
        if not await asyncio.to_thread(self.upload_sessions.__contains__,user_id):
            return (None, '❌ لم تبدأ جلسة رفع محتوى')
        if not description_text:
            return (None, "❌ يرجى إرسال وصف نصي أو اكتب 'بدون'")
        session = await asyncio.to_thread(self.get_user_session, user_id)
        if not session or not await asyncio.to_thread(db.is_admin, user_id) or await asyncio.to_thread(db.is_banned, user_id):
            await asyncio.to_thread(self.cancel_upload, user_id)
            return (None, 'انتهت جلسة الرفع أو الصلاحية.')
        if len(description_text) > 500:
            return (None, 'الوصف يجب ألا يتجاوز 500 حرف.')
        if description_text.lower() == 'بدون':
            session['description'] = None
        else:
            session['description'] = description_text
        session['step'] = 'content'
        await asyncio.to_thread(self.upload_sessions.__setitem__,user_id,session)
        content_type_names = {'video': '🎥 الآن، أرسل الفيديو:', 'document': '📄 الآن، أرسل الملف:', 'photo': '🖼️ الآن، أرسل الصورة:', 'text': '📝 الآن، أرسل النص:', 'audio': '🎵 الآن، أرسل الملف الصوتي:', 'voice': '🎤 الآن، أرسل البصمة الصوتية:'}
        return (content_type_names[session['content_type']], None)

    async def process_content(self, user_id, message):
        """معالجة المحتوى المرسل"""
        if not await asyncio.to_thread(self.upload_sessions.__contains__,user_id):
            return '❌ لم تبدأ جلسة رفع محتوى'
        session = await asyncio.to_thread(self.get_user_session, user_id)
        if not session or not await asyncio.to_thread(db.is_admin, user_id) or await asyncio.to_thread(db.is_banned, user_id):
            await asyncio.to_thread(self.cancel_upload, user_id)
            return 'انتهت جلسة الرفع أو الصلاحية.'
        content_type = session['content_type']
        try:
            file_id = None
            storage_message_id = None
            original_name = None
            text = None
            if content_type == 'text':
                text = getattr(message, 'message', None) or getattr(message, 'text', None) or getattr(message, 'raw_text', None)
                if len(text or '') > 3000:
                    return 'النص يجب ألا يتجاوز 3000 حرف.'
                if not text:
                    return '❌ لم يتم إرسال نص'
            else:
                if not getattr(message, 'media', None):
                    return '❌ لم تقم بإرسال الوسائط المطلوبة'
                if not getattr(message, content_type, None):
                    return 'نوع الملف لا يطابق النوع المختار.'
                media_size = getattr(getattr(message, 'document', None), 'size', None)
                if media_size and media_size > MAX_UPLOAD_BYTES:
                    return f'❌ حجم الملف أكبر من الحد المسموح ({MAX_UPLOAD_MB}MB)'
                doc = getattr(message, 'document', None)
                if doc:
                    if getattr(doc, 'file_name', None):
                        original_name = doc.file_name
                    else:
                        for attr in getattr(doc, 'attributes', []) or []:
                            if getattr(attr, 'file_name', None):
                                original_name = attr.file_name
                                break
                file_id = telethon_utils.pack_bot_file_id(message.media)
                if not file_id:
                    return '❌ تعذر حفظ معرف الملف في Telegram'
                if STORAGE_CHANNEL_ID:
                    try:
                        stored_message = await client.send_file(STORAGE_CHANNEL_ID, message.media)
                        stored_file_id = telethon_utils.pack_bot_file_id(stored_message.media)
                        if stored_file_id:
                            file_id = stored_file_id
                            storage_message_id = stored_message.id
                    except Exception as e:
                        return 'تعذر تنفيذ العملية. حاول لاحقاً.'
            content_id = await asyncio.to_thread(db.finish_upload, user_id, stage=session['stage'], subject_key=session['subject_key'], chapter_num=session['chapter_num'], content_type=content_type, file_id=file_id, file_name=original_name, text=text, description=session['description'], added_by=user_id, storage_message_id=storage_message_id)
            if content_id:
                content_data = await asyncio.to_thread(db.get_stage_content_by_id, content_id)
                content_number = content_data[10] if content_data else content_id
                subject = await asyncio.to_thread(db.get_stage_subject, session['stage'], session['subject_key'])
                stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][session['stage'] - 1]
                # The transactional outbox was created by finish_upload.
                user = None
                notification_msg = '\n📢 أضيفت الإشعارات إلى قائمة الإرسال.'
                label = await asyncio.to_thread(db.chapter_label, session['stage'], session['subject_key'], session['chapter_num'])
                return f"✅ تم رفع المحتوى بنجاح!\n📚 المادة: {subject[0]}\n🎓 المرحلة: {stage_name}\n📖 {label}\n📝 الوصف: {session['description'] or 'لا يوجد وصف'}\n🔢 الرقم التسلسلي: #{content_number}{notification_msg}"
            else:
                return '❌ فشل في حفظ المحتوى'
        except Exception as e:
            logging.exception('Content upload failed')
            return '❌ تعذر إتمام الرفع. تحقق من قائمة المحتوى قبل إعادة المحاولة.'

    def cancel_upload(self, user_id):
        """إلغاء جلسة الرفع"""
        if user_id in self.upload_sessions:
            del self.upload_sessions[user_id]
            return '✅ تم إلغاء عملية الرفع'
        return '❌ لا توجد جلسة رفع نشطة'

    def get_user_session(self, user_id):
        """الحصول على جلسة المستخدم"""
        session = self.upload_sessions.get(user_id)
        if session and time.time() - session.get('at', 0) > 900:
            self.upload_sessions.pop(user_id, None)
            return None
        return session
content_upload_handler = ContentUploadHandler()

async def connect_assistant():
    if user_client is None:return False
    if not user_client.is_connected():await asyncio.wait_for(user_client.connect(),15)
    if not await asyncio.wait_for(user_client.is_user_authorized(),10):
        await user_client.disconnect()
        raise RuntimeError('Assistant session is no longer authorized; replace SESSION1')
    return True

class ChannelSearch:

    @staticmethod
    async def search_in_telegram_channels(query, channels, stage, limit_per_channel=None):
        """البحث باستخدام جلسة المستخدم للحصول على نتائج أفضل"""
        limit_per_channel = min(50,max(1,int(limit_per_channel or 30)))
        results = []
        accessible_channels = []
        if user_client is None:
            return ([], '❌ لا توجد جلسة حساب مساعد مهيّئة. يرجى ضبط متغير البيئة SESSION1. 🔧 ثم أعد تشغيل البوت حتى تتمكن ميزة البحث من العمل باستخدام حساب المساعد.')
        try:
            if not user_client.is_connected():
                await connect_assistant()
        except Exception as e:
            return ([], 'تعذر تنفيذ العملية. حاول لاحقاً.')
        search_client = user_client
        for channel_info in channels:
            try:
                channel_entity = await search_client.get_entity(channel_info[1])
                async for message in search_client.iter_messages(channel_entity, limit=1):
                    pass
                accessible_channels.append(channel_info)
            except (ChannelInvalidError, ChannelPrivateError, ValueError) as e:
                print(f'❌ لا يمكن الوصول للقناة {channel_info[1]}: {e}')
                continue
            except Exception as e:
                print('تعذر تنفيذ العملية. حاول لاحقاً.')
                continue
        if not accessible_channels:
            return ([], '❌ لا يمكن الوصول إلى أي قناة بحث باستخدام حساب المساعد. تأكد من أن الحساب المساعد عضو/مشرف في القنوات المدرجة.')
        tasks = []
        for channel_info in accessible_channels:
            task = ChannelSearch.search_in_single_channel_user_session(search_client, query, channel_info, stage, limit_per_channel)
            tasks.append(task)
        semaphore = asyncio.Semaphore(3)
        async def bounded(task):
            async with semaphore:
                return await asyncio.wait_for(task,20)
        channel_results = await asyncio.gather(*(bounded(task) for task in tasks), return_exceptions=True)
        for result in channel_results:
            if isinstance(result, list):
                results.extend(result)
        results.sort(key=lambda x: x.get('relevance', 0), reverse=True)
        failures = sum((not isinstance(x, list) for x in channel_results))
        status = f'اكتمل البحث في {len(accessible_channels) - failures} قناة من أصل {len(channels)}'
        if failures or len(accessible_channels) < len(channels):
            status += ' — بعض القنوات غير متاحة أو لم يكتمل البحث فيها.'
        return (results, status)

    @staticmethod
    async def search_in_single_channel_user_session(search_client, query, channel_info, stage, limit):
        """البحث في قناة فردية باستخدام جلسة المستخدم"""
        channel_results = []
        try:
            channel_entity = await search_client.get_entity(channel_info[1])
            stage_keywords = {1: ['أولى', '1', 'first', 'الصف الأول'], 2: ['ثانية', '2', 'second', 'الصف الثاني'], 3: ['ثالثة', '3', 'third', 'الصف الثالث'], 4: ['رابعة', '4', 'fourth', 'الصف الرابع']}
            stage_words = stage_keywords.get(stage, [])
            async for message in search_client.iter_messages(channel_entity, search=query, limit=min(50,int(limit or 30))):
                if message.text:
                    relevance = ChannelSearch.calculate_relevance(message.text, query, stage_words, stage)
                    if relevance >= 0:
                        username = getattr(channel_entity, 'username', None)
                        message_link = f'https://t.me/{username}/{message.id}' if username else f'https://t.me/c/{channel_entity.id}/{message.id}'
                        channel_results.append({'channel_title': channel_info[2], 'channel_username': channel_info[1], 'message_text': message.text[:200] + '...' if len(message.text) > 200 else message.text, 'message_id': message.id, 'date': message.date, 'relevance': relevance, 'stage': stage, 'message_link': message_link})
                        if limit is not None and len(channel_results) >= limit:
                            break
        except Exception as e:
            raise RuntimeError('لم يكتمل البحث في القناة ' + str(channel_info[1])) from e
        return channel_results

    @staticmethod
    def calculate_relevance(text, query, stage_words, stage):
        """حساب درجة الصلة بين النتيجة والبحث"""
        text_lower = text.lower()
        query_lower = query.lower()
        relevance = 0
        query_words = query_lower.split()
        matched_words = 0
        for word in query_words:
            if len(word) > 2 and word in text_lower:
                relevance += 3
                matched_words += 1
        if matched_words >= len(query_words) * 0.7:
            relevance += 5
        for stage_word in stage_words:
            if stage_word.lower() in text_lower:
                relevance += 8
                break
        educational_terms = ['امتحان', 'شرح', 'ملخص', 'تمرين', 'حل', 'مادة', 'درس', 'أسئلة', 'إجابات']
        for term in educational_terms:
            if term in text:
                relevance += 2
        if any((word in text_lower for word in ['بحث', 'دراسة', 'تحليل', 'منهج'])):
            relevance += 1
        return relevance

    @staticmethod
    async def check_channel_access(channel_username):
        """التحقق من إمكانية وصول الحساب المستخدم للقناة"""
        try:
            search_client = client
            if user_client is not None:
                try:
                    if not user_client.is_connected():
                        await connect_assistant()
                    if user_client.is_connected():
                        search_client = user_client
                except Exception as e:
                    print(f'Warning: failed to start user_client in check_channel_access: {e}')
            channel_entity = await search_client.get_entity(channel_username)
            await search_client.get_messages(channel_entity, limit=1)
            return (True, '✅ القناة متاحة')
        except ChannelPrivateError:
            return (False, '❌ القناة خاصة أو الحساب المستخدم ليس عضوًا')
        except ChannelInvalidError:
            return (False, '❌ المعرف غير صحيح')
        except ValueError:
            return (False, '❌ لم يتم العثور على القناة')
        except Exception as e:
            msg = str(e)
            if 'GetHistoryRequest' in msg or 'API access for bot users' in msg or ('restricted' in msg and 'bot' in msg):
                return (False, f'⚠️ ❌ خطأ: البوت لا يملك صلاحية الوصول لعرض تاريخ القناة ({channel_username}).\n\n📌 الحل: أضف البوت كمسؤول في القناة @{channel_username} أو شغّل جلسة حساب مساعد (SESSION1) لتفادي قيود البوت.')
            return (False, f'❌ خطأ: {msg}')

    @staticmethod
    async def initialize_user_session():
        """Compatibility entry point for admin/integration callers."""
        global user_client
        if not user_client or user_client.is_connected():
            return user_client is not None
        try:
            await connect_assistant()
            return True
        except Exception:
            user_client = None
            return False

class SubscriptionUnavailable(RuntimeError):
    pass

_subscription_cache = {}
_subscription_gate = asyncio.Semaphore(4)

async def check_subscription(user_id):
    if user_id == DEVELOPER_ID: return True
    channels = await asyncio.to_thread(db.get_required_channels)
    if not channels: return True
    key = (user_id,tuple(str(row[1]) for row in channels))
    cached = _subscription_cache.get(key)
    if cached and cached[0]>time.monotonic(): return cached[1]
    async def check(channel):
        async with _subscription_gate:
            return await _is_subscribed_to_channel(user_id,channel[1])
    try:
        results = await asyncio.wait_for(asyncio.gather(*(check(c) for c in channels),return_exceptions=True),15)
    except asyncio.TimeoutError:
        raise SubscriptionUnavailable('Subscription check timed out') from None
    if False in results: result = False
    elif any(isinstance(x,BaseException) for x in results):
        raise SubscriptionUnavailable('Telegram membership check is temporarily unavailable')
    else: result = True
    if len(_subscription_cache)>10000: _subscription_cache.clear()
    _subscription_cache[key]=(time.monotonic()+(30 if result else 10),result)
    return result

async def _is_subscribed_to_channel(user_id,channel):
    from telethon.errors import UserNotParticipantError
    from telethon.tl import types
    for candidate in (client,user_client):
        if candidate is None or not candidate.is_connected(): continue
        try:
            permissions = await asyncio.wait_for(candidate.get_permissions(channel,user_id),8)
            participant = getattr(permissions,'participant',None)
            if isinstance(participant,types.ChannelParticipantLeft): return False
            if isinstance(participant,types.ChannelParticipantBanned):
                return not bool(participant.left or getattr(participant.banned_rights,'view_messages',False))
            return permissions is not None
        except UserNotParticipantError: return False
        except Exception: continue
    raise SubscriptionUnavailable('No client could check membership')

async def send_subscription_message(chat_id):
    """إرسال روابط جميع قنوات الاشتراك الإجباري."""
    required_channels = await asyncio.to_thread(db.get_required_channels)
    if not required_channels:
        return False
    buttons = [[Button.url(f'📢 {title}', f'https://t.me/{username}')] for _, _, username, title in required_channels]
    buttons.append([Button.inline('✅ لقد اشتركت في جميع القنوات', 'check_subscription')])
    await client.send_message(chat_id, '📢 يرجى الاشتراك في جميع القنوات التالية أولاً لتتمكن من استخدام البوت:', buttons=buttons)
    return True
