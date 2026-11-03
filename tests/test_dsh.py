import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import server

class BridgeTests(unittest.IsolatedAsyncioTestCase):
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
        ids = []
        async def page(sid):
            ids.append(sid)
            return object()
        server.is_ready = True
        with patch.object(server, 'get_or_create_session_page', side_effect=page), \
             patch.object(server, 'delete_session', new=AsyncMock()) as cleanup, \
             patch.object(server, 'send_to_gemini', new=AsyncMock(return_value='{"tool_calls":[{"name":"exec","arguments":{"cmd":"pwd"}}]}')):
            for _ in range(2):
                response = await server.chat_completions(request)
                events = ''.join([chunk async for chunk in response.body_iterator])
                self.assertIn('"finish_reason": "tool_calls"', events)
                self.assertIn('[DONE]', events)
            self.assertEqual(len(set(ids)), 2)
            self.assertEqual(cleanup.await_count, 2)

    async def test_unknown_tools_fail_before_dispatch_and_cleanup(self):
        request = server.ChatRequest(model='gemini-web', tools=[{'type': 'function', 'function': {'name': 'read'}}], messages=[{'role': 'user', 'content': 'test'}])
        server.is_ready = True
        with patch.object(server, 'get_or_create_session_page', new=AsyncMock()), \
             patch.object(server, 'delete_session', new=AsyncMock()) as cleanup, \
             patch.object(server, 'send_to_gemini', new=AsyncMock(return_value='{"tool_calls":[{"name":"write","arguments":{}}]}')):
            with self.assertRaises(server.HTTPException) as caught:
                await server.chat_completions(request)
            self.assertEqual(caught.exception.status_code, 502)
            cleanup.assert_awaited_once()

    async def test_image_input_is_rejected_before_opening_a_page(self):
        request = server.ChatRequest(model='gemini-web', messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}])
        server.is_ready = True
        with patch.object(server, 'get_or_create_session_page', new=AsyncMock()) as page:
            with self.assertRaises(server.HTTPException) as caught:
                await server.chat_completions(request)
            self.assertEqual(caught.exception.status_code, 400)
            page.assert_not_awaited()

if __name__ == '__main__': unittest.main()
