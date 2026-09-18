import asyncio
import os
from telethon import utils as telethon_utils
from styled_buttons import Button
from telethon.errors import ChannelInvalidError, ChannelPrivateError
from telethon.errors import FloodWaitError
from config import client, user_client, DEVELOPER_ID, STORAGE_CHANNEL_ID
from database import db

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '200'))
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
            subject = db.get_stage_subject(stage, subject_key)
            subject_name = subject[0] if subject else "مادة غير معروفة"

            caption = f"📝 الوصف: {description}\n\n" if description else ""
            stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

            full_caption = f"{caption}📚 {subject_name} | الفصل {chapter_num} | 🎓 المرحلة {stage_name} | 🔢 #{content_number}"

            if content_type != 'text':
                media_source = file_id
                storage_message_id = db.get_content_storage_message_id(content_id)
                if STORAGE_CHANNEL_ID and storage_message_id:
                    stored_message = await client.get_messages(STORAGE_CHANNEL_ID, ids=storage_message_id)
                    if not stored_message or not stored_message.media:
                        raise ValueError("ملف قناة التخزين غير موجود")
                    media_source = stored_message.media
                elif not isinstance(file_id, str) or telethon_utils.resolve_bot_file_id(file_id) is None:
                    raise ValueError("المحتوى لا يحتوي على معرف ملف Telegram صالح")

            if content_type == 'video':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'document':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'photo':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'text' and text:
                await client.send_message(chat_id, f"{full_caption}\n\n{text}")
            elif content_type == 'audio':
                await client.send_file(chat_id, media_source, caption=full_caption.strip())
            elif content_type == 'voice':
                await client.send_file(chat_id, media_source, caption=full_caption.strip(), voice_note=True)
            else:
                await client.send_message(chat_id, f"❌ نوع المحتوى غير معروف: {content_type}")

            if is_admin:
                buttons = [
                    [Button.inline("🗑️ حذف هذا المحتوى", f'stage_content:delete:{content_id}')],
                    [Button.inline("العودة للفصل", f'stage_chapter:{stage}:{subject_key}:{chapter_num}')]
                ]
                await client.send_message(chat_id, "خيارات الأدمن:", buttons=buttons)
            else:
                favorite_text = "⭐ إزالة من المفضلة" if db.is_favorite(chat_id, content_id) else "⭐ إضافة للمفضلة"
                buttons = [
                    [Button.inline(favorite_text, f'learning:favorite:{content_id}')],
                    [Button.inline("💬 عرض التعليقات", f'content_comments:view:{content_id}')],
                    [Button.inline("➕ إضافة تعليق", f'content_comments:add:{content_id}')],
                    [Button.inline("العودة", f'stage_chapter:{stage}:{subject_key}:{chapter_num}')]
                ]
                await client.send_message(chat_id, "خيارات المحتوى:", buttons=buttons)

            return True
        except Exception as e:
            print(f"Error sending content: {e}")
            await client.send_message(chat_id, f"❌ خطأ في عرض المحتوى: {e}")
            return False

    @staticmethod
    async def send_stage_content_list(chat_id, stage, subject_key, chapter_num, content_list, is_admin=False, show_chapter=True):
        """إرسال قائمة محتوى الفصل"""
        subject = db.get_stage_subject(stage, subject_key)
        subject_name = subject[0] if subject else "مادة غير معروفة"
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        chapter_label = f" - الفصل {chapter_num}" if show_chapter else ""
        message = f"📂 محتوى {subject_name}{chapter_label} (المرحلة {stage_name}):\n\n"

        if not content_list:
            message += "⚠️ لا يوجد محتوى متاح لهذا الفصل بعد."
            await client.send_message(chat_id, message)
            return

        for i, (content_id, content_type, description, date_added, content_number) in enumerate(content_list, 1):
            icon = '🎥' if content_type == 'video' else '📄' if content_type == 'document' else '🖼️' if content_type == 'photo' else '📝' if content_type == 'text' else '🎵' if content_type == 'audio' else '🎤'
            desc_display = description[:30] + "..." if description and len(description) > 30 else description or f"محتوى #{content_number}"
            message += f"{i}. {icon} {desc_display} (#{content_number})\n"

        buttons = []
        for content_id, content_type, description, date_added, content_number in content_list:
            icon = '🎥' if content_type == 'video' else '📄' if content_type == 'document' else '🖼️' if content_type == 'photo' else '📝' if content_type == 'text' else '🎵' if content_type == 'audio' else '🎤'
            btn_text = f"{icon} #{content_number} - {description[:20]}..." if description else f"{icon} محتوى #{content_number}"
            buttons.append([Button.inline(btn_text, f'stage_content:view:{content_id}')])

        if is_admin:
            buttons.append([Button.inline("➕ إضافة محتوى", f'stage_content:add:{stage}:{subject_key}:{chapter_num}')])

        subject = db.get_stage_subject(stage, subject_key)
        buttons.append([Button.inline("العودة", ContentSender._category_back_callback(stage, subject[3]))])

        await client.send_message(chat_id, message, buttons=buttons)

    @staticmethod
    async def notify_new_stage_content(stage, subject_key, chapter_num, content_type, description, added_by_name):
        """Notify all users about new content in specific stage"""
        subject = db.get_stage_subject(stage, subject_key)
        if not subject:
            return False, 0

        content_type_names = {
            'video': 'فيديو',
            'document': 'ملف',
            'photo': 'صورة',
            'text': 'نص',
            'audio': 'ملف صوتي',
            'voice': 'بصمة صوتية'
        }

        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]

        message = (
            f"📢 إشعار جديد (المرحلة {stage_name}):\n"
            f"قام الأدمن {added_by_name} برفع {content_type_names.get(content_type, 'محتوى')} جديد\n"
            f"📚 لـ {subject[0]} - الفصل {chapter_num}\n"
            f"📝 الوصف: {description if description else 'لا يوجد وصف'}\n\n"
            f"🔍 استعرض المحتوى من خلال البوت"
        )

        users = db.get_all_users()
        success = 0
        failures = 0

        batch_size = 20
        delay_between_batches = 1.0

        batch = []
        for uid in users:
            user_stage_data = db.get_user_stage(uid)
            if not user_stage_data or user_stage_data[0] != stage:
                continue
            if not db.notifications_enabled(uid):
                continue
            batch.append(uid)

            if len(batch) >= batch_size:
                for user_id in batch:
                    try:
                        await client.send_message(user_id, message)
                        success += 1
                    except FloodWaitError as fe:
                        wait = getattr(fe, 'seconds', None) or 5
                        print(f"FloodWait: sleeping {wait}s")
                        await asyncio.sleep(wait)
                        try:
                            await client.send_message(user_id, message)
                            success += 1
                        except Exception as e:
                            print(f"Failed after floodwait to {user_id}: {e}")
                            failures += 1
                    except Exception as e:
                        print(f"Failed to send notification to {user_id}: {e}")
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
                    print(f"FloodWait: sleeping {wait}s")
                    await asyncio.sleep(wait)
                    try:
                        await client.send_message(user_id, message)
                        success += 1
                    except Exception as e:
                        print(f"Failed after floodwait to {user_id}: {e}")
                        failures += 1
                except Exception as e:
                    print(f"Failed to send notification to {user_id}: {e}")
                    failures += 1

        return success, failures

class ContentUploadHandler:
    """معالج رفع المحتوى الجديد"""

    def __init__(self):
        self.upload_sessions = {}

    async def start_upload_session(self, user_id, stage, subject_key, chapter_num, content_type):
        """بدء جلسة رفع محتوى جديدة"""
        self.upload_sessions[user_id] = {
            'stage': stage,
            'subject_key': subject_key,
            'chapter_num': chapter_num,
            'content_type': content_type,
            'step': 'description'
        }

        subject = db.get_stage_subject(stage, subject_key)
        stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][stage-1]
        content_type_names = {
            'video': 'فيديو',
            'document': 'ملف',
            'photo': 'صورة',
            'text': 'نص',
            'audio': 'ملف صوتي',
            'voice': 'بصمة صوتية'
        }

        message = (
            f"⬆️ بدء رفع {content_type_names[content_type]}:\n"
            f"📚 المادة: {subject[0]}\n"
            f"🎓 المرحلة: {stage_name}\n"
            f"📖 الفصل: {chapter_num}\n\n"
            f"📝 الرجاء إرسال الوصف أولاً (أو اكتب 'بدون' لعدم إضافة وصف):"
        )

        return message

    async def process_description(self, user_id, description_text):
        """معالجة وصف المحتوى"""
        if user_id not in self.upload_sessions:
            return None, "❌ لم تبدأ جلسة رفع محتوى"
        if not description_text:
            return None, "❌ يرجى إرسال وصف نصي أو اكتب 'بدون'"

        session = self.upload_sessions[user_id]

        if description_text.lower() == 'بدون':
            session['description'] = None
        else:
            session['description'] = description_text

        session['step'] = 'content'

        content_type_names = {
            'video': '🎥 الآن، أرسل الفيديو:',
            'document': '📄 الآن، أرسل الملف:',
            'photo': '🖼️ الآن، أرسل الصورة:',
            'text': '📝 الآن، أرسل النص:',
            'audio': '🎵 الآن، أرسل الملف الصوتي:',
            'voice': '🎤 الآن، أرسل البصمة الصوتية:'
        }

        return content_type_names[session['content_type']], None

    async def process_content(self, user_id, message):
        """معالجة المحتوى المرسل"""
        if user_id not in self.upload_sessions:
            return "❌ لم تبدأ جلسة رفع محتوى"

        session = self.upload_sessions[user_id]
        content_type = session['content_type']

        try:
            file_id = None
            storage_message_id = None
            original_name = None
            text = None

            if content_type == 'text':
                text = getattr(message, 'message', None) or getattr(message, 'text', None) or getattr(message, 'raw_text', None)
                if not text:
                    return "❌ لم يتم إرسال نص"

            else:
                if not getattr(message, 'media', None):
                    return "❌ لم تقم بإرسال الوسائط المطلوبة"

                media_size = getattr(getattr(message, 'document', None), 'size', None)
                if media_size and media_size > MAX_UPLOAD_BYTES:
                    return f"❌ حجم الملف أكبر من الحد المسموح ({MAX_UPLOAD_MB}MB)"

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
                    return "❌ تعذر حفظ معرف الملف في Telegram"
                if STORAGE_CHANNEL_ID:
                    try:
                        stored_message = await client.send_file(STORAGE_CHANNEL_ID, message.media)
                        stored_file_id = telethon_utils.pack_bot_file_id(stored_message.media)
                        if stored_file_id:
                            file_id = stored_file_id
                            storage_message_id = stored_message.id
                    except Exception as e:
                        return f"❌ فشل الحفظ في قناة التخزين: {e}"

            content_id = db.add_stage_content(
                stage=session['stage'],
                subject_key=session['subject_key'],
                chapter_num=session['chapter_num'],
                content_type=content_type,
                file_id=file_id,
                file_name=original_name,
                text=text,
                description=session['description'],
                added_by=user_id,
                storage_message_id=storage_message_id
            )

            if content_id:
                content_data = db.get_stage_content_by_id(content_id)
                content_number = content_data[10] if content_data else content_id

                subject = db.get_stage_subject(session['stage'], session['subject_key'])
                stage_name = ['أولى', 'ثانية', 'ثالثة', 'رابعة'][session['stage']-1]

                user = await client.get_entity(user_id)
                added_by_name = f"{user.first_name} {user.last_name}" if user.first_name else f"@{user.username}" if user.username else f"المستخدم {user_id}"

                success, failures = await ContentSender.notify_new_stage_content(
                    session['stage'], session['subject_key'], session['chapter_num'],
                    content_type, session['description'], added_by_name
                )

                del self.upload_sessions[user_id]

                notification_msg = ""
                if success > 0:
                    notification_msg = f"\n📢 تم إرسال إشعار لـ {success} مستخدم"

                return (
                    f"✅ تم رفع المحتوى بنجاح!\n"
                    f"📚 المادة: {subject[0]}\n"
                    f"🎓 المرحلة: {stage_name}\n"
                    f"📖 الفصل: {session['chapter_num']}\n"
                    f"📝 الوصف: {session['description'] or 'لا يوجد وصف'}\n"
                    f"🔢 الرقم التسلسلي: #{content_number}"
                    f"{notification_msg}"
                )
            else:
                return "❌ فشل في حفظ المحتوى"

        except Exception as e:
            return f"❌ خطأ في رفع المحتوى: {str(e)}"

    def cancel_upload(self, user_id):
        """إلغاء جلسة الرفع"""
        if user_id in self.upload_sessions:
            del self.upload_sessions[user_id]
            return "✅ تم إلغاء عملية الرفع"
        return "❌ لا توجد جلسة رفع نشطة"

    def get_user_session(self, user_id):
        """الحصول على جلسة المستخدم"""
        return self.upload_sessions.get(user_id)

content_upload_handler = ContentUploadHandler()

class ChannelSearch:
    @staticmethod
    async def search_in_telegram_channels(query, channels, stage, limit_per_channel=5):
        """البحث باستخدام جلسة المستخدم للحصول على نتائج أفضل"""
        results = []
        accessible_channels = []
        if user_client is None:
            return [], ("❌ لا توجد جلسة حساب مساعد مهيّئة. يرجى ضبط متغير البيئة SESSION1. "
                      "🔧 ثم أعد تشغيل البوت حتى تتمكن ميزة البحث من العمل باستخدام حساب المساعد.")
        try:
            if not user_client.is_connected():
                await user_client.start()
        except Exception as e:
            return [], (f"❌ فشل في بدء جلسة الحساب المساعد: {e}\n🔧 تأكد أن SESSION1 صالح ثم أعد التشغيل.")

        search_client = user_client

        for channel_info in channels:
            try:
                channel_entity = await search_client.get_entity(channel_info[1])
                async for message in search_client.iter_messages(channel_entity, limit=1):
                    pass
                accessible_channels.append(channel_info)
            except (ChannelInvalidError, ChannelPrivateError, ValueError) as e:
                print(f"❌ لا يمكن الوصول للقناة {channel_info[1]}: {e}")
                continue
            except Exception as e:
                print(f"⚠️ خطأ في التحقق من القناة {channel_info[1]}: {e}")
                continue

        if not accessible_channels:
            return [], "❌ لا يمكن الوصول إلى أي قناة بحث باستخدام حساب المساعد. تأكد من أن الحساب المساعد عضو/مشرف في القنوات المدرجة."

        tasks = []
        for channel_info in accessible_channels:
            task = ChannelSearch.search_in_single_channel_user_session(search_client, query, channel_info, stage, limit_per_channel)
            tasks.append(task)

        channel_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in channel_results:
            if isinstance(result, list):
                results.extend(result)

        results.sort(key=lambda x: x.get('relevance', 0), reverse=True)

        return results, f"✅ تم البحث في {len(accessible_channels)} قناة من أصل {len(channels)}"

    @staticmethod
    async def search_in_single_channel_user_session(search_client, query, channel_info, stage, limit):
        """البحث في قناة فردية باستخدام جلسة المستخدم"""
        channel_results = []
        try:
            channel_entity = await search_client.get_entity(channel_info[1])

            stage_keywords = {
                1: ['أولى', '1', 'first', 'الصف الأول'],
                2: ['ثانية', '2', 'second', 'الصف الثاني'],
                3: ['ثالثة', '3', 'third', 'الصف الثالث'],
                4: ['رابعة', '4', 'fourth', 'الصف الرابع']
            }

            stage_words = stage_keywords.get(stage, [])

            async for message in search_client.iter_messages(
                channel_entity,
                search=query,
                limit=limit * 10
            ):
                if message.text:
                    relevance = ChannelSearch.calculate_relevance(
                        message.text, query, stage_words, stage
                    )

                    if relevance >= 5:
                        message_link = f"https://t.me/{channel_info[1]}/{message.id}"

                        channel_results.append({
                            'channel_title': channel_info[2],
                            'channel_username': channel_info[1],
                            'message_text': message.text[:200] + "..." if len(message.text) > 200 else message.text,
                            'message_id': message.id,
                            'date': message.date,
                            'relevance': relevance,
                            'stage': stage,
                            'message_link': message_link
                        })

                        if len(channel_results) >= limit:
                            break

        except Exception as e:
            print(f"خطأ في البحث في القناة {channel_info[1]}: {e}")

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

        if any(word in text_lower for word in ['بحث', 'دراسة', 'تحليل', 'منهج']):
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
                        await user_client.start()
                    if user_client.is_connected():
                        search_client = user_client
                except Exception as e:
                    print(f"Warning: failed to start user_client in check_channel_access: {e}")

            channel_entity = await search_client.get_entity(channel_username)
            await search_client.get_messages(channel_entity, limit=1)
            return True, "✅ القناة متاحة"
        except ChannelPrivateError:
            return False, "❌ القناة خاصة أو الحساب المستخدم ليس عضوًا"
        except ChannelInvalidError:
            return False, "❌ المعرف غير صحيح"
        except ValueError:
            return False, "❌ لم يتم العثور على القناة"
        except Exception as e:
            msg = str(e)
            if 'GetHistoryRequest' in msg or 'API access for bot users' in msg or 'restricted' in msg and 'bot' in msg:
                return False, (
                    f"⚠️ ❌ خطأ: البوت لا يملك صلاحية الوصول لعرض تاريخ القناة ({channel_username}).\n\n"
                    f"📌 الحل: أضف البوت كمسؤول في القناة @{channel_username} أو شغّل جلسة حساب مساعد (SESSION1) لتفادي قيود البوت."
                )
            return False, f"❌ خطأ: {msg}"

    @staticmethod
    async def initialize_user_session():
        """Compatibility entry point for admin/integration callers."""
        global user_client
        if not user_client or user_client.is_connected():
            return user_client is not None
        try:
            await user_client.start()
            return True
        except Exception:
            user_client = None
            return False

async def check_subscription(user_id):
    """التحقق من اشتراك المستخدم في جميع القنوات الإلزامية."""
    if user_id == DEVELOPER_ID:
        return True

    required_channels = db.get_required_channels()
    if not required_channels:
        return True

    for _, _, username, _ in required_channels:
        if not await _is_subscribed_to_channel(user_id, username):
            return False
    return True

async def _is_subscribed_to_channel(user_id, username):
    """التحقق من الاشتراك في قناة واحدة باستخدام العميل المتاح."""

    clients_to_try = []
    if 'user_client' in globals() and user_client:
        clients_to_try.append(user_client)
    clients_to_try.append(client)

    for c in clients_to_try:
        try:
            if not c.is_connected():
                await c.start()

            try:
                await c.get_participant(username, user_id)
                return True
            except Exception:
                pass

            try:
                perm = await c.get_permissions(username, user_id)
                if perm is not None:
                    if hasattr(perm, 'is_user'):
                        return bool(getattr(perm, 'is_user'))
                    return True
            except Exception:
                pass

        except Exception:
            continue

    return False

async def send_subscription_message(chat_id):
    """إرسال روابط جميع قنوات الاشتراك الإجباري."""
    required_channels = db.get_required_channels()
    if not required_channels:
        return False

    buttons = [
        [Button.url(f"📢 {title}", f"https://t.me/{username}")]
        for _, _, username, title in required_channels
    ]
    buttons.append([Button.inline("✅ لقد اشتركت في جميع القنوات", "check_subscription")])

    await client.send_message(
        chat_id,
        "📢 يرجى الاشتراك في جميع القنوات التالية أولاً لتتمكن من استخدام البوت:",
        buttons=buttons
    )
    return True
