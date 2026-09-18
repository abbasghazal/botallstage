"""Project-wide colored Telegram button helpers."""

from telethon import Button as TelethonButton
from telethon.tl import types


def _button_style(text, data=None):
    """Choose a semantic color while keeping callbacks unchanged."""
    label = str(text or '')
    callback = str(data or '')
    value = f'{label} {callback}'.lower()

    positive_exceptions = ('إلغاء حظر', 'unban')
    if any(token in value for token in positive_exceptions):
        return 'success'

    danger_tokens = (
        '🗑', '❌', '🚫', 'حذف', 'إزالة', 'إلغاء', 'حظر', 'تعطيل',
        ':delete', ':remove', ':cancel', ':ban_', ':toggle:0',
    )
    if any(token in value for token in danger_tokens):
        return 'danger'

    success_tokens = (
        '➕', '✅', 'إضافة', 'رفع', 'تفعيل', 'تأكيد', 'اشتركت',
        ':add', ':upload', ':toggle:1', ':reply', 'check_subscription',
    )
    if any(token in value for token in success_tokens):
        return 'success'

    return 'primary'


class Button:
    """Drop-in subset of Telethon's Button API with automatic colors."""

    @staticmethod
    def inline(text, data=None, style=None, icon=None):
        return TelethonButton.inline(
            text,
            data,
            style=style or _button_style(text, data),
            icon=icon,
        )

    @staticmethod
    def url(text, url=None, style=None, icon=None):
        return TelethonButton.url(
            text,
            url,
            style=style or _button_style(text, url),
            icon=icon,
        )

    @staticmethod
    def web_app(text, url, style=None):
        chosen_style = style or _button_style(text, url)
        native_web_app = getattr(TelethonButton, 'web_app', None)
        if native_web_app:
            try:
                return native_web_app(text, url, style=chosen_style)
            except TypeError:
                return native_web_app(text, url)

        button_type = getattr(types, 'KeyboardButtonWebView', None)
        if button_type is None:
            button_type = getattr(types, 'KeyboardButtonSimpleWebView', None)
        if button_type is None:
            return Button.url(text, url, style=chosen_style)

        get_style = getattr(TelethonButton, '_get_style', None)
        resolved_style = get_style(chosen_style) if get_style else chosen_style
        try:
            return button_type(text=text, url=url, style=resolved_style)
        except TypeError:
            return button_type(text=text, url=url)
