import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import server


class FakePage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def install_sessions(pages):
    """Stand in for the browser: register a fake tab per requested session."""
    async def fake_session(session_id, start_new_chat=False):
        page = FakePage()
        pages.append(page)
        server.session_pages[session_id] = page
        server.page_locks[session_id] = asyncio.Lock()
        return page
    return fake_session


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server.is_ready = True
        server.conversations.clear()
        server.session_pages.clear()
        server.page_locks.clear()
        server.model_choices.clear()
        server.startup_task = None

    def test_json_arguments_preserve_braces_and_brackets(self):
        args = {'code': 'if (a[0]) { console.log("}"); }'}
        calls = server.parse_tool_calls(json.dumps({'tool_calls': [{'name': 'exec', 'arguments': args}]}))
        self.assertEqual(calls[0]['arguments'], args)
        self.assertIsNone(server.parse_tool_calls('Example: {"tool_calls": []}'))

    async def test_text_history_and_sse_tool_round_trip(self):
        tool = {'type': 'function', 'function': {'name': 'exec', 'parameters': {'type': 'object'}}}
        request = server.ChatRequest(model='gemini-web', stream=True, tools=[tool], messages=[
            {'role': 'developer', 'content': 'Keep instructions'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'exec', 'arguments': '{"cmd":"id"}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '1000'},
            {'role': 'user', 'content': 'continue'},
        ])
        prompt = server.format_conversation(request.messages, request.tools)
        self.assertIn('Keep instructions', prompt)
        self.assertIn('call_id=c1', prompt)
        pages = []
        with patch.object(server, 'ensure_ready', new=AsyncMock()), \
             patch.object(server, 'get_or_create_session_page', side_effect=install_sessions(pages)), \
             patch.object(server, 'send_to_gemini', new=AsyncMock(return_value='{"tool_calls":[{"name":"exec","arguments":{"cmd":"pwd"}}]}')):
            for _ in range(2):
                response = await server.chat_completions(request)
                events = ''.join([chunk async for chunk in response.body_iterator])
                self.assertIn('"finish_reason": "tool_calls"', events)
                self.assertIn('[DONE]', events)
        # Replaying the same history is not a follow-up turn: each call gets a
        # fresh tab and the previous tab is closed instead of leaking.
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[0].closed)
        self.assertEqual(len(server.conversations), 1)

    async def test_follow_up_turn_reuses_tab_and_sends_only_new_messages(self):
        pages = []
        first = server.ChatRequest(model='gemini-web', messages=[
            {'role': 'system', 'content': 'You are terse'},
            {'role': 'user', 'content': 'first question'},
        ])
        second = server.ChatRequest(model='gemini-web', messages=[
            {'role': 'system', 'content': 'You are terse'},
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'answer one'},
            {'role': 'user', 'content': 'second question'},
        ])
        sent = []

        async def fake_send(page, text, timeout=None):
            sent.append(text)
            return 'answer one' if len(sent) == 1 else 'answer two'

        with patch.object(server, 'ensure_ready', new=AsyncMock()), \
             patch.object(server, 'get_or_create_session_page', side_effect=install_sessions(pages)), \
             patch.object(server, 'send_to_gemini', side_effect=fake_send):
            await server.chat_completions(first)
            await server.chat_completions(second)

        self.assertEqual(len(pages), 1)
        self.assertIn('first question', sent[0])
        self.assertIn('Continue this same chat', sent[1])
        self.assertNotIn('first question', sent[1])
        self.assertIn('second question', sent[1])
        self.assertEqual(server.conversations[list(server.conversations)[0]]['consumed'], 4)

    async def test_changed_earlier_turn_rebuilds_and_closes_stale_tab(self):
        pages = []
        first = server.ChatRequest(model='gemini-web', messages=[{'role': 'user', 'content': 'first question'}])
        edited = server.ChatRequest(model='gemini-web', messages=[
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'a different answer'},
            {'role': 'user', 'content': 'second question'},
        ])
        with patch.object(server, 'ensure_ready', new=AsyncMock()), \
             patch.object(server, 'get_or_create_session_page', side_effect=install_sessions(pages)), \
             patch.object(server, 'send_to_gemini', new=AsyncMock(return_value='answer one')):
            await server.chat_completions(first)
            await server.chat_completions(edited)
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[0].closed)
        self.assertFalse(pages[1].closed)

    def test_long_history_is_compressed_without_dropping_recent_turns(self):
        messages = [server.Message(role='system', content='rules')]
        for i in range(40):
            messages.append(server.Message(role='user', content=f'question {i} ' + 'x' * 500))
            messages.append(server.Message(role='assistant', content=f'answer {i} ' + 'y' * 500))
        messages.append(server.Message(role='user', content='latest question'))
        full = server.format_conversation(messages)
        prompt = server.format_conversation(messages, budget=8000)
        self.assertLess(len(prompt), len(full))
        self.assertIn('축약했습니다', prompt)
        self.assertIn('System Instructions:\nrules', prompt)
        self.assertIn('latest question', prompt)
        self.assertIn('answer 37 ' + 'y' * 500, prompt)
        self.assertNotIn('answer 30 ' + 'y' * 500, prompt)

    async def test_unknown_tools_fail_before_dispatch_and_cleanup(self):
        request = server.ChatRequest(model='gemini-web', tools=[{'type': 'function', 'function': {'name': 'read'}}], messages=[{'role': 'user', 'content': 'test'}])
        pages = []
        with patch.object(server, 'ensure_ready', new=AsyncMock()), \
             patch.object(server, 'get_or_create_session_page', side_effect=install_sessions(pages)), \
             patch.object(server, 'close_session', new=AsyncMock()) as cleanup, \
             patch.object(server, 'send_to_gemini', new=AsyncMock(return_value='{"tool_calls":[{"name":"write","arguments":{}}]}')):
            with self.assertRaises(server.HTTPException) as caught:
                await server.chat_completions(request)
            self.assertEqual(caught.exception.status_code, 502)
            cleanup.assert_awaited_once()
        self.assertEqual(server.conversations, {})

    async def test_image_input_is_rejected_before_opening_a_page(self):
        request = server.ChatRequest(model='gemini-web', messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}])
        with patch.object(server, 'ensure_ready', new=AsyncMock()) as ready, \
             patch.object(server, 'get_or_create_session_page', new=AsyncMock()) as page:
            with self.assertRaises(server.HTTPException) as caught:
                await server.chat_completions(request)
            self.assertEqual(caught.exception.status_code, 400)
            page.assert_not_awaited()
            ready.assert_not_awaited()

    async def test_expired_login_reports_manual_login_stage(self):
        server.is_ready = False
        missing = Path(tempfile.mkdtemp()) / 'logged-in'
        with patch.object(server, 'browser_alive', new=AsyncMock(return_value=False)), \
             patch.object(server, 'stop_browser', new=AsyncMock()), \
             patch.object(server, 'LOGIN_FLAG', missing):
            with self.assertRaises(server.HTTPException) as caught:
                await server.ensure_ready()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(server.login_stage, 'manual-login')

    async def test_dead_browser_is_reconnected_before_serving(self):
        server.is_ready = False
        alive = AsyncMock(return_value=False)
        with patch.object(server, 'browser_alive', new=alive), \
             patch.object(server, 'stop_browser', new=AsyncMock()) as stop, \
             patch.object(server, 'connect_context', new=AsyncMock()) as connect, \
             patch.object(server, 'LOGIN_FLAG', Path(tempfile.mkdtemp()) / 'logged-in') as flag:
            flag.write_text('ok')
            await server.ensure_ready()
        stop.assert_awaited_once()
        connect.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
