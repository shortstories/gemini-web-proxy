import unittest
from playwright.async_api import async_playwright
import server

class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content('''<div class="model-picker-container"><button onclick="document.querySelector('#menu').hidden=false">Fast</button></div>
        <div id="menu" hidden>
        <button role="menuitem" data-mode-id="aaa" class="selected" data-active="false" onclick="selectMode('Fast','aaa')">Fast<br>Quick answers</button>
        <button role="menuitem" data-mode-id="bbb" data-active="true" onclick="selectMode('Pro','bbb')">Pro<br>Reasoning</button>
        <button role="menuitem" data-mode-id="ccc" disabled>Unavailable</button>
        <button role="menuitem">High<br>Thinking depth, not a model</button></div>
        <script>function selectMode(name,id){document.querySelectorAll('[data-mode-id]').forEach(e=>e.classList.toggle('selected',e.getAttribute('data-mode-id')===id));document.querySelector('.model-picker-container button').textContent=name;document.querySelector('#menu').hidden=true}
        document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('#menu').hidden=true})</script>''')
        server.model_choices.clear()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()
        server.model_choices.clear()

    async def test_only_enabled_model_entries_are_discovered(self):
        choices = await server.discover_modes(self.page)
        self.assertEqual(set(choices), {'gemini-web-aaa', 'gemini-web-bbb'})
        self.assertEqual(choices['gemini-web-bbb']['web_label'], 'Pro')

    async def test_selected_dsh_model_changes_the_browser_picker(self):
        server.model_choices.update(await server.discover_modes(self.page))
        await server.choose_mode(self.page, 'gemini-web-bbb')
        self.assertEqual(await self.page.locator('.model-picker-container button').inner_text(), 'Pro')
        await server.choose_mode(self.page, 'gemini-web-aaa')
        self.assertEqual(await self.page.locator('.model-picker-container button').inner_text(), 'Fast')

    async def test_unknown_mode_is_refused(self):
        with self.assertRaises(server.HTTPException) as caught:
            await server.choose_mode(self.page, 'gemini-web-missing')
        self.assertEqual(caught.exception.status_code, 404)
