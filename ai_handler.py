"""Simple optional OpenAI integration using aiohttp and usage tracking.

Features:
- check daily usage per user
- record usage
- call OpenAI Chat Completions (gpt-3.5-turbo by default)
- graceful fallback when OPENAI_API_KEY is missing
"""
import os
import aiohttp
import asyncio
from datetime import datetime, date
from typing import Optional, Dict, Any
from config import OPENAI_API_KEY
from database import db

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo').strip() or 'gpt-3.5-turbo'
try:
    DAILY_TOKEN_LIMIT = max(1, int(os.environ.get('OPENAI_DAILY_LIMIT', '4000')))
except ValueError:
    DAILY_TOKEN_LIMIT = 4000
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=10)
MAX_QUESTION_LENGTH = 1500


async def _call_openai(prompt: str, system: Optional[str] = None, max_tokens: int = 512) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError('OpenAI API key is not configured')

    headers = {
        'Authorization': f'Bearer {OPENAI_API_KEY}',
        'Content-Type': 'application/json'
    }

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        'model': MODEL,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': 0.2
    }

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(OPENAI_API_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f'OpenAI API returned HTTP {resp.status}')
            return await resp.json()


def _today_iso() -> str:
    return date.today().isoformat()


def get_user_daily_usage(user_id: int) -> int:
    """Return total tokens used today by user."""
    usage = db._read_data('ai_usage') or {}
    if not isinstance(usage, dict):
        usage = {}
    today = _today_iso()
    user_key = str(user_id)
    user_record = usage.get(user_key, {})
    return int(user_record.get('daily_tokens', {}).get(today, 0))


def record_user_usage(user_id: int, prompt_tokens: int, completion_tokens: int):
    """Record tokens used by user for today."""
    today = _today_iso()
    user_key = str(user_id)
    added = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
    def mutate(usage):
        if not isinstance(usage, dict):
            return
        user_record = usage.get(user_key, {'daily_tokens': {}})
        daily = user_record.get('daily_tokens', {})
        daily[today] = int(daily.get(today, 0)) + added
        user_record['daily_tokens'] = daily
        user_record['last_used'] = datetime.now().isoformat()
        usage[user_key] = user_record
    db._mutate_data('ai_usage', mutate)


async def summarize_text(user_id: int, text: str) -> str:
    """Summarize text using OpenAI while checking daily quota."""
    if not OPENAI_API_KEY:
        return "⚠️ ميزة الذكاء الاصطناعي غير مفعّلة. يرجى ضبط `OPENAI_API_KEY`."

    current = get_user_daily_usage(user_id)
    if current >= DAILY_TOKEN_LIMIT:
        return "⚠️ تم تجاوز الحدّ اليومي لاستهلاك الذكاء الاصطناعي. حاول لاحقًا."

    system = "أنت مساعد تعليمي متخصص بتلخيص المحتوى الدراسي باختصار وبأسلوب بسيط للطلاب."
    try:
        resp = await _call_openai(prompt=text, system=system, max_tokens=400)
        choices = resp.get('choices') or []
        if not choices:
            raise RuntimeError('No choices from OpenAI')
        message = choices[0].get('message', {}).get('content', '')

        usage_info = resp.get('usage', {})
        prompt_tokens = usage_info.get('prompt_tokens', 0)
        completion_tokens = usage_info.get('completion_tokens', 0)
        record_user_usage(user_id, prompt_tokens, completion_tokens)

        return message.strip()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "❌ تعذر الاتصال بخدمة الذكاء الاصطناعي. حاول لاحقاً."
    except Exception:
        return "❌ تعذر تنفيذ طلب الذكاء الاصطناعي. حاول لاحقاً."


async def explain_question(user_id: int, question: str, stage: int = None) -> str:
    if not OPENAI_API_KEY:
        return "⚠️ ميزة الذكاء الاصطناعي غير مفعّلة."
    question = str(question or '').strip()
    if not 3 <= len(question) <= MAX_QUESTION_LENGTH:
        return "⚠️ السؤال غير صالح أو طويل جداً."
    if get_user_daily_usage(user_id) >= DAILY_TOKEN_LIMIT:
        return "⚠️ تم تجاوز الحدّ اليومي لاستهلاك الذكاء الاصطناعي."
    context_parts = []
    for content, subject_name in db.search_content(question, stage, limit=8):
        value = content.get('text') or content.get('description') or content.get('file_name') or ''
        if value:
            context_parts.append(f"المادة: {subject_name}\nالمحتوى: {value}")
    context = '\n\n'.join(context_parts)[:6000]
    prompt = f"سؤال الطالب:\n{question}\n\nمحتوى ذو صلة من البوت:\n{context or 'لا يوجد سياق محلي مطابق.'}"
    system = "أنت مساعد تعليمي. اعتمد على السياق المرفق عندما يكون مفيدًا، واذكر اسم المادة. لا تخترع معلومات من السياق."
    try:
        response = await _call_openai(prompt, system=system, max_tokens=700)
        message = (response.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
        usage = response.get('usage', {})
        record_user_usage(user_id, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))
        return message or "❌ لم يصل رد من النموذج."
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "❌ تعذر الاتصال بخدمة الذكاء الاصطناعي. حاول لاحقاً."
    except Exception:
        return "❌ تعذر تنفيذ طلب الذكاء الاصطناعي. حاول لاحقاً."
