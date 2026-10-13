"""Regressions for /start membership checks, without Telegram connections."""
import itertools
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from telethon.errors import ChatAdminRequiredError, UserNotParticipantError
from telethon.tl import types as tl
from test_regressions import db, main, utils
import security


class StartSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    user_ids = itertools.count(600000000)

    async def asyncSetUp(self):
        self.uid = next(self.user_ids)
        self.channel_id = 1234567890
        self.channel = tl.Channel(
            id=self.channel_id, title='Required', photo=tl.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc), access_hash=98765)
        self.peer = tl.InputPeerChannel(self.channel_id, 98765)
        self.bot = types.SimpleNamespace(
            is_connected=lambda: True,
            get_input_entity=AsyncMock(return_value=self.peer),
            get_entity=AsyncMock(return_value=self.channel),
            get_permissions=AsyncMock(return_value=types.SimpleNamespace(
                participant=object(), is_admin=True)))
        self.channels = [(1, str(self.channel_id), 'required_channel', 'Required')]
        self.event = types.SimpleNamespace(
            sender_id=self.uid, chat_id=self.uid, is_private=True,
            raw_text='/start', reply=AsyncMock(), answer=AsyncMock(), edit=AsyncMock())
        utils._subscription_cache.clear()
        self.patches = [
            patch.object(utils, 'client', self.bot),
            patch.object(utils, 'user_client', None),
            patch.object(db, 'get_required_channels', return_value=self.channels),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(utils._subscription_cache.clear)

    async def test_start_with_legacy_string_channel_id_opens_registration(self):
        await main.handle_start(self.event)
        self.assertIn('اسمك الثلاثي', self.event.reply.await_args.args[0])
        peer = self.bot.get_input_entity.await_args.args[0]
        self.assertIsInstance(peer, tl.PeerChannel)
        self.assertEqual(peer.channel_id, self.channel_id)
        self.bot.get_permissions.assert_awaited_once_with(self.peer, self.uid)

    async def test_marked_channel_id_is_resolved_as_a_channel(self):
        self.channels[0] = (1, str(-1000000000000 - self.channel_id), 'required_channel', 'Required')
        self.assertTrue(await utils.check_subscription(self.uid))
        self.assertEqual(self.bot.get_input_entity.await_args.args[0].channel_id, self.channel_id)

    async def test_fresh_session_resolves_username_and_opens_menu(self):
        self.bot.get_input_entity.side_effect = ValueError('Missing access hash')
        db.add_user(self.uid, None, 'Test', 'User')
        db.set_user_stage(self.uid, 'Test User', 1)
        await main.handle_start(self.event)
        self.bot.get_entity.assert_awaited_once_with('@required_channel')
        self.bot.get_permissions.assert_awaited_once_with(self.channel, self.uid)
        self.assertIn('Test User', self.event.reply.await_args.args[0])
        self.assertIn('buttons', self.event.reply.await_args.kwargs)

    async def test_username_reassigned_to_another_channel_does_not_grant_access(self):
        self.bot.get_input_entity.side_effect = ValueError('Missing access hash')
        self.channel.id += 1
        with self.assertRaises(utils.SubscriptionUnavailable):
            await utils.check_subscription(self.uid)
        self.bot.get_permissions.assert_not_awaited()

    async def test_non_member_is_sent_subscription_prompt(self):
        self.bot.get_permissions.side_effect = UserNotParticipantError(None)
        with patch.object(utils, 'send_subscription_message', new=AsyncMock()) as prompt:
            await main.handle_start(self.event)
        prompt.assert_awaited_once_with(self.uid)
        self.event.reply.assert_not_awaited()

    async def test_left_member_is_not_subscribed(self):
        self.bot.get_permissions.return_value.participant = tl.ChannelParticipantLeft(tl.PeerUser(self.uid))
        self.assertFalse(await utils.check_subscription(self.uid))

    async def test_membership_failure_stays_unavailable_and_is_not_cached(self):
        self.bot.get_permissions.side_effect = ChatAdminRequiredError(None)
        with self.assertRaises(utils.SubscriptionUnavailable):
            await utils.check_subscription(self.uid)
        self.bot.get_permissions.side_effect = None
        self.assertTrue(await utils.check_subscription(self.uid))
        self.assertEqual(self.bot.get_permissions.await_count, 2)

    async def test_assistant_can_check_when_bot_cannot(self):
        self.bot.get_permissions.side_effect = ChatAdminRequiredError(None)
        assistant = types.SimpleNamespace(
            is_connected=lambda: True,
            get_input_entity=AsyncMock(return_value=self.peer),
            get_permissions=AsyncMock(return_value=types.SimpleNamespace(participant=object())))
        with patch.object(utils, 'user_client', assistant):
            self.assertTrue(await utils.check_subscription(self.uid))
        assistant.get_permissions.assert_awaited_once_with(self.peer, self.uid)

    async def test_confirmation_button_rechecks_cached_non_membership(self):
        self.bot.get_permissions.side_effect = UserNotParticipantError(None)
        self.assertFalse(await utils.check_subscription(self.uid))
        self.bot.get_permissions.side_effect = None
        self.event.data = b'check_subscription'
        self.assertTrue(await security.guard_event(self.event))
        self.assertEqual(self.bot.get_permissions.await_count, 2)

    async def test_generic_message_handler_does_not_guard_commands_again(self):
        with patch.object(main, 'guard_event', new=AsyncMock(return_value=False)) as guard:
            await main.handle_upload_messages(self.event)
        guard.assert_not_awaited()
        self.event.reply.assert_not_awaited()

    async def test_channel_without_bot_admin_permissions_is_not_saved(self):
        self.event.sender_id = 123
        self.event.raw_text = '@required_channel'
        self.bot.get_permissions.return_value.is_admin = False
        with patch.object(main, 'client', self.bot), patch.object(db, 'add_required_channel') as save:
            await main.process_add_channel(self.event)
        save.assert_not_called()
        self.assertIn('كمسؤول', self.event.reply.await_args.args[0])

    async def test_new_required_channel_is_saved_after_admin_check(self):
        self.event.sender_id = 123
        self.event.raw_text = '@required_channel'
        with patch.object(main, 'client', self.bot), patch.object(db, 'add_required_channel', return_value=True) as save:
            await main.process_add_channel(self.event)
        self.bot.get_permissions.assert_awaited_once_with(self.channel, 'me')
        self.assertEqual(save.call_args.kwargs['channel_id'], str(self.channel_id))


if __name__ == '__main__':
    unittest.main()
