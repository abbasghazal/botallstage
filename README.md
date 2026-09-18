# bot4stage

بوت تعليمي للمراحل الأربع مبني على Telethon.

## الميزات

- محتوى دراسي محفوظ بمعرفات Telegram دون ملفات وسائط محلية.
- PostgreSQL للإنتاج على Render مع SQLite للتشغيل المحلي.
- اختبارات، درجات، لوحة متفوقين، مفضلة وتتبع تقدم.
- بحث داخلي وإشعارات قابلة للتعطيل وجدولة محتوى.
- نسخ احتياطي يومي وسجل عمليات وإحصائيات تعلم.
- إجابات ذكاء اصطناعي مدعومة بسياق المحتوى.
- قناة تخزين وMini App اختياريتان.

## التشغيل المحلي

1. انسخ `.env.example` إلى `.env` وأدخل القيم المطلوبة؛ لا ترفع ملف `.env` إلى Git.
2. ثبّت المتطلبات ثم شغّل البوت:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

على Windows استبدل مسارات `.venv/bin` بـ `.venv\\Scripts`.

## متغيرات البيئة

المتغيرات التالية مطلوبة لتشغيل البوت: `API_ID` و`API_HASH` و`BOT_TOKEN`.
أما `DATABASE_URL` فهو مطلوب في الإنتاج؛ ويُسمح بـSQLite محليًا في بيئة التطوير فقط.

```env
DATABASE_PATH=bot.db
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
BACKUP_PATH=backups
STORAGE_CHANNEL_ID=-1001234567890
MINI_APP_URL=https://example.com
MINI_APP_PORT=8080
```

يجب إضافة البوت مشرفًا في قناة التخزين. يحتاج Mini App رابط HTTPS عام يشير إلى المنفذ المحدد. على Render المجاني يجب ضبط `DATABASE_URL` من مزود PostgreSQL خارجي دائم.

## النشر على Render

الملف `render.yaml` جاهز للاستيراد. بعد ربط المستودع، أضف القيم الحساسة من لوحة Render ولا تضعها في Git:

- `API_ID`, `API_HASH`, `BOT_TOKEN`
- `DATABASE_URL` (PostgreSQL دائم مع SSL عند الحاجة)
- `DEVELOPER_ID`, `STORAGE_CHANNEL_ID`
- اختياريًا: `SESSION1`, `OPENAI_API_KEY`, `MINI_APP_URL`

سيستخدم Render متغير `PORT` الذي يوفّره تلقائيًا، ويكون فحص الصحة على `/health`. اضبط `MINI_APP_URL` على رابط HTTPS للخدمة قبل استخدام زر Mini App.

## أوامر الإدارة

```text
/adminhelp
/addquiz المرحلة | subject_key | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الإجابة الصحيحة
/schedule CONTENT_ID YYYY-MM-DD HH:MM
```

## ملفات الرفع

ارفع ملفات المصدر ومجلد `web` مع `requirements.txt` و`render.yaml` و`.env.example`.
لا تتضمن نسخة النشر قاعدة بيانات SQLite محلية أو جلسة Telegram أو ملف `.env`؛ أدخل الأسرار من إعدادات الاستضافة، واضبط `DATABASE_URL` لقاعدة PostgreSQL الدائمة.
للتشغيل المحلي مجددًا، أنشئ `.env` من `.env.example` وأدخل إعداداتك. ملفات الجلسة وقاعدة SQLite المحلية تُنشأ أثناء التشغيل بحسب الإعدادات.
