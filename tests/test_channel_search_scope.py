import time
import types
import unittest
from unittest.mock import AsyncMock,patch
from test_regressions import db,main,utils

class ChannelSearchScopeTests(unittest.IsolatedAsyncioTestCase):
    def event(self):
        self.uid=97000000+int(time.time_ns()%1000000)
        return types.SimpleNamespace(sender_id=self.uid,chat_id=self.uid,raw_text='ليزر',reply=AsyncMock(),edit=AsyncMock(),answer=AsyncMock())
    async def test_channel_only_with_all_results_and_three_pages(self):
        e=self.event()
        channels=[(1,'searchone','قناة الأولى',4),(2,'searchtwo','قناة الثانية',4)]
        remote=[{'message_link':f'https://t.me/searchone/{i+1}','channel_title':'قناة الأولى','message_text':'ليزر'} for i in range(12)]
        with patch.object(db,'search_content',side_effect=AssertionError('Local search forbidden')),patch.object(db,'get_search_channels',return_value=channels) as getchannels,patch.object(utils.ChannelSearch,'search_in_telegram_channels',new=AsyncMock(return_value=(remote,'اكتمل البحث'))) as search:
            await main.process_stage_search_query(e,4)
        getchannels.assert_called_once_with(4)
        search.assert_awaited_once_with('ليزر',channels,4,limit_per_channel=30)
        session=main.search_sessions[self.uid]
        self.assertEqual(session['results'],remote)
        for page,count in ((0,5),(1,5),(2,2)):
            await main.display_results_page(e,session,page)
            buttons=[b for row in e.reply.await_args.kwargs['buttons'] for b in row]
            urls=[b for b in buttons if getattr(b.type,'url',None)]
            self.assertEqual(len(urls),count)
            callbacks=[b.type.data for b in buttons if hasattr(b.type,'data')]
            self.assertFalse(any(x.startswith(b'stage_content:view:') for x in callbacks))
            self.assertEqual(b'search_page:'+str(page+1).encode() in callbacks,page<2)
            self.assertEqual(b'search_page:'+str(page-1).encode() in callbacks,page>0)
    async def test_no_channels_has_no_local_fallback(self):
        e=self.event()
        with patch.object(db,'search_content',side_effect=AssertionError('Local search forbidden')),patch.object(db,'get_search_channels',return_value=[]),patch.object(utils.ChannelSearch,'search_in_telegram_channels',new=AsyncMock()) as search:
            await main.process_stage_search_query(e,4)
        search.assert_not_awaited()
        self.assertEqual(main.search_sessions[self.uid]['results'],[])
        self.assertIn('لا توجد قنوات بحث',e.reply.await_args.args[0])
    async def test_channel_failure_shows_status_without_local_fallback(self):
        e=self.event()
        with patch.object(db,'search_content',side_effect=AssertionError('Local search forbidden')),patch.object(db,'get_search_channels',return_value=[(1,'private','قناة',4)]),patch.object(utils.ChannelSearch,'search_in_telegram_channels',new=AsyncMock(return_value=([],'تعذر الوصول إلى القناة'))):
            await main.process_stage_search_query(e,4)
        self.assertEqual(main.search_sessions[self.uid]['results'],[])
        self.assertIn('تعذر الوصول',e.reply.await_args.args[0])
    async def test_content_search_remains_independent(self):
        e=self.event()
        local=[({'id':123,'text':'ليزر','content_type':'text'},'مادة الليزر')]
        with patch.object(db,'search_content',return_value=local) as search,patch.object(utils.ChannelSearch,'search_in_telegram_channels',new=AsyncMock(side_effect=AssertionError('Remote search forbidden'))):
            await main.process_content_search(e,4)
        search.assert_called_once_with('ليزر',4,limit=None)
        self.assertEqual(main.search_sessions[self.uid]['results'][0]['content_id'],123)
