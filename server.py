from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Union, Annotated
import json
import asyncio
import time
import re
import os
import uuid
import hmac
import shutil
import hashlib
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page
from markdownify import markdownify as md

app = FastAPI()

# Service profile
SERVICE_DIR = Path(os.environ.get("GEMINI_WEB_STATE", str(Path.home() / ".gemini-service")))
PROFILE_DIR = SERVICE_DIR / "chrome-profile"
LOGIN_FLAG = SERVICE_DIR / "logged-in"
INTERACTIVE_REQUEST = SERVICE_DIR / "interactive-request"
LOGIN_COMPLETE = SERVICE_DIR / "login-complete"

# Global state
context: BrowserContext = None
playwright_instance = None
is_ready = False
session_pages: Dict[str, Page] = {}
page_locks: Dict[str, asyncio.Lock] = {}
session_msg_count: Dict[str, int] = {}
request_lock = asyncio.Lock()
reconnect_lock = asyncio.Lock()
startup_task = None
cleanup_task = None
manual_browser = None
login_stage = "starting"
login_error = None
MODEL_ID = "gemini-web"
model_choices = {}

# Conversation continuation. A parked entry keeps a filled Gemini tab alive so
# later turns of the same harness conversation only have to send new messages.
conversations: Dict[str, Dict[str, Any]] = {}
CONVERSATION_TTL = 1800      # seconds an idle tab stays reusable
MAX_PARKED_TABS = 6          # hard cap on live conversations
HISTORY_CHAR_BUDGET = 36000  # a full re-send is compressed beyond this size
KEEP_RECENT_FULL = 8         # newest messages never clipped by that compression
CLIP_CHARS = 240             # per-message clip for older turns

@app.middleware("http")
async def local_auth(request, call_next):
    from fastapi.responses import JSONResponse
    token_file = os.environ.get("GEMINI_WEB_TOKEN_FILE")
    if token_file and request.url.path != "/health":
        expected = Path(token_file).read_text().strip()
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


class Message(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict] = None


class Tool(BaseModel):
    type: str
    function: FunctionDef


class ChatRequest(BaseModel):
    model: str = "gemini-web"
    messages: List[Message]
    stream: Optional[bool] = False
    timeout: Optional[int] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stream_options: Optional[Dict[str, Any]] = None
    top_p: Optional[float] = None
    user: Optional[str] = None
    sessionId: Optional[str] = None
    conversation_id: Optional[str] = None

def get_content_text(content: Any) -> str:
    """Helper to extract text from string or list content"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join([
            c.get("text", "") 
            for c in content 
            if isinstance(c, dict) and c.get("type") == "text"
        ])
    return ""

async def check_logged_in(page: Page) -> bool:
    # An editor can also exist for anonymous visitors. Require an account cookie.
    cookies = await page.context.cookies("https://gemini.google.com")
    account = any(c["name"] in ("SID", "__Secure-1PSID", "__Secure-3PSID") for c in cookies)
    return account and await page.locator("rich-textarea").count() > 0


async def init_browser():
    global context, playwright_instance, is_ready, manual_browser, login_stage, login_error
    SERVICE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    playwright_instance = await async_playwright().start()
    while True:
        interactive = not LOGIN_FLAG.exists() or INTERACTIVE_REQUEST.exists()
        if interactive:
            login_stage = "manual-login"
            # Authentication runs in ordinary Chrome without Playwright,
            # WebDriver, a debugging endpoint, or automation flags.
            manual_browser = await asyncio.create_subprocess_exec(
                shutil.which("google-chrome") or "/usr/bin/google-chrome",
                "--user-data-dir=" + str(PROFILE_DIR), "--no-first-run",
                "--window-size=1280,900", "--disable-dev-shm-usage",
                "https://gemini.google.com/app",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            print("Ordinary Chrome is waiting for manual Google sign-in.", flush=True)
            while not LOGIN_COMPLETE.exists():
                if manual_browser.returncode is not None:
                    # Closing the login window is recoverable via the DSH button.
                    login_stage = "window-closed"
                    return
                await asyncio.sleep(0.5)
            login_stage = "validating"
            if manual_browser.returncode is None:
                manual_browser.terminate()
                try:
                    await asyncio.wait_for(manual_browser.wait(), timeout=15)
                except asyncio.TimeoutError:
                    manual_browser.kill()
                    await manual_browser.wait()
            manual_browser = None
            await asyncio.sleep(1)
            INTERACTIVE_REQUEST.unlink(missing_ok=True)
            LOGIN_COMPLETE.unlink(missing_ok=True)
        try:
            await connect_context()
        except Exception:
            return
        print("Gemini web subscription is ready (headless).", flush=True)
        return


async def connect_context():
    """Launch the signed-in profile and verify the stored Google session."""
    global context, is_ready, login_stage, login_error
    context = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), headless=True, channel="chrome",
        ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
        args=["--disable-extensions", "--no-first-run", "--disable-dev-shm-usage"],
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)
    if not await check_logged_in(page):
        LOGIN_FLAG.unlink(missing_ok=True)
        await context.close()
        context = None
        login_stage = "verification-failed"
        login_error = "저장된 Google 로그인을 확인하지 못했습니다. 자동 재로그인은 중지했습니다."
        raise RuntimeError("stored Google login could not be verified")
    LOGIN_FLAG.write_text("ok")
    await page.close()
    is_ready = True
    login_stage = "connected"
    login_error = None


def mode_id(label, web_id=None):
    if web_id and re.fullmatch(r"[a-zA-Z0-9-]+", web_id):
        return "gemini-web-" + web_id.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return "gemini-web-" + (slug or hashlib.sha256(label.encode()).hexdigest()[:10])


async def mode_items(page):
    trigger = page.locator('.model-picker-container button').first
    await trigger.wait_for(timeout=20000)
    await trigger.click()
    await page.wait_for_timeout(400)
    for selector in ['[data-mode-id]', '[data-test-id^="bard-mode-option-"]']:
        items = page.locator(selector)
        visible = [item for item in await items.all() if await item.is_visible()]
        if visible:
            return visible
    await page.keyboard.press("Escape")
    raise HTTPException(502, "Gemini 웹 모드 메뉴를 읽지 못했습니다.")


async def discover_modes(page):
    choices = {}
    try:
        for item in await mode_items(page):
            if not await item.is_enabled() or await item.get_attribute("aria-disabled") == "true":
                continue
            lines = [line.strip() for line in (await item.inner_text()).splitlines() if line.strip()]
            if not lines:
                continue
            label = lines[0]
            web_id = await item.get_attribute("data-mode-id")
            identity = mode_id(label, web_id)
            choices[identity] = {"id": identity, "name": "Gemini Web · " + label, "web_label": label, "web_id": web_id}
    finally:
        await page.keyboard.press("Escape")
    if not choices:
        raise HTTPException(502, "계정에서 선택할 수 있는 Gemini 웹 모드를 찾지 못했습니다.")
    return choices


async def choose_mode(page, model):
    if model == MODEL_ID:
        return
    if model not in model_choices:
        model_choices.update(await discover_modes(page))
    choice = model_choices.get(model)
    if choice is None:
        raise HTTPException(404, "사용할 수 없는 Gemini 웹 모드입니다. 목록을 새로 고쳐 주세요.")
    for item in await mode_items(page):
        lines = [line.strip() for line in (await item.inner_text()).splitlines() if line.strip()]
        if lines and (await item.get_attribute("data-mode-id") == choice.get("web_id") if choice.get("web_id") else lines[0] == choice["web_label"]) and await item.is_enabled():
            await item.click()
            await page.wait_for_timeout(300)
            # The picker button may abbreviate its label (e.g. "Flash" for
            # "3.8 Flash"). Verify the stable web identity in the menu instead.
            for attempt in range(10):
                confirmed = False
                for current in await mode_items(page):
                    if choice.get("web_id") and await current.get_attribute("data-mode-id") == choice["web_id"]:
                        confirmed = ("selected" in (await current.get_attribute("class") or "").split()
                                     or await current.get_attribute("aria-checked") == "true")
                    elif not choice.get("web_id"):
                        selected = await page.locator('.model-picker-container button').first.inner_text()
                        confirmed = choice["web_label"] in selected
                    if confirmed:
                        break
                await page.keyboard.press("Escape")
                if confirmed:
                    return
                await page.wait_for_timeout(200)
            raise HTTPException(502, "Gemini 웹 모드 전환을 확인하지 못했습니다.")
    await page.keyboard.press("Escape")
    raise HTTPException(409, "선택한 Gemini 웹 모드가 현재 계정 메뉴에 없습니다.")


async def get_or_create_session_page(session_id: str, start_new_chat: bool = False) -> Page:
    global session_pages, page_locks
    
    if session_id not in session_pages:
        print(f"  → New session: {session_id}")
        
        page = await context.new_page()
        await page.goto('https://gemini.google.com/app')
        await asyncio.sleep(2)
        
        await page.wait_for_selector('rich-textarea', timeout=15000)
        
        # Always start fresh for new session
        try:
            new_chat_btn = await page.query_selector('side-nav-action-button[data-test-id="new-chat-button"]')
            if new_chat_btn:
                await new_chat_btn.click()
                await asyncio.sleep(1)
        except:
            pass
        
        await page.wait_for_selector('rich-textarea', timeout=10000)
        session_pages[session_id] = page
        page_locks[session_id] = asyncio.Lock()
        print(f"  ✓ Session {session_id} ready")
    else:
        # Check if page is still valid
        try:
            _ = session_pages[session_id].url
        except:
            del session_pages[session_id]
            if session_id in page_locks:
                del page_locks[session_id]
            return await get_or_create_session_page(session_id, start_new_chat)
        
        # Start new chat only if explicitly requested (new conversation)
        if start_new_chat:
            print(f"  → Starting new chat in session {session_id}")
            try:
                page = session_pages[session_id]
                new_chat_btn = await page.query_selector('side-nav-action-button[data-test-id="new-chat-button"]')
                if new_chat_btn:
                    await new_chat_btn.click()
                    await asyncio.sleep(1)
                    await page.wait_for_selector('rich-textarea', timeout=10000)
            except:
                pass
    
    return session_pages[session_id]


def format_tools_prompt(tools: List[Tool]) -> str:
    if not tools:
        return ""
    definitions = [t.model_dump() for t in tools]
    return (
        "The following conversation is a request from an external assistant harness. "
        "Follow its system instructions and answer the latest user message. "
        "The harness executes tools; you only request them. To request tools, return ONLY "
        'a JSON object {"tool_calls":[{"name":"declared_name","arguments":{}}]}. '
        "Use exactly the declared names and schemas, with complete JSON arguments. "
        "Never use placeholders for source code. Wait for tool results before claiming success. "
        "For a final answer, return normal text. Available tools:\n"
        + json.dumps(definitions, ensure_ascii=False)
    )


def _message_line(msg: Message, clip: int = 0) -> Optional[str]:
    role = msg.role
    content = get_content_text(msg.content)
    if clip and role not in ("system", "developer") and len(content) > clip:
        content = content[:clip] + " …(생략)"
    if role in ("system", "developer"):
        return f"System Instructions:\n{content}"
    if role == "user":
        return f"User: {content}"
    if role == "assistant":
        if msg.tool_calls:
            # Format previous tool calls
            tc_str = json.dumps({"tool_calls": [
                {"name": tc.get("function", {}).get("name"),
                 "arguments": json.loads(tc.get("function", {}).get("arguments", "{}"))}
                for tc in msg.tool_calls
            ]})
            return f"Assistant: {tc_str}"
        if content:
            return f"Assistant: {content}"
        return None
    if role == "tool":
        tool_name = msg.name or "tool"
        return f"Tool Result ({tool_name}, call_id={msg.tool_call_id}):\n{content}"
    return None


def _render(messages: List[Message], tools: Optional[List[Tool]], clips: Dict[int, int]) -> str:
    formatted = [format_tools_prompt(tools)] if tools else []
    for index, msg in enumerate(messages):
        line = _message_line(msg, clips.get(index, 0))
        if line:
            formatted.append(line)
    return "\n\n".join(formatted)


def format_conversation(messages: List[Message], tools: Optional[List[Tool]] = None,
                        budget: Optional[int] = None) -> str:
    """Format messages into a prompt, compressing older turns when over budget."""
    prompt = _render(messages, tools, {})
    if budget is None or len(prompt) <= budget:
        return prompt
    recent = max(0, len(messages) - KEEP_RECENT_FULL)
    clipped = _render(messages, tools, {index: CLIP_CHARS for index in range(recent)})
    return (
        "[앞선 대화는 길이 때문에 축약했습니다. 축약 구간의 세부 내용이 필요하면 추측하지 말고 사용자에게 확인하세요.]\n\n"
        + clipped
    )


def message_key(msg: Message) -> str:
    return "\x00".join([
        msg.role or "",
        msg.name or "",
        msg.tool_call_id or "",
        get_content_text(msg.content),
        json.dumps(msg.tool_calls, sort_keys=True, ensure_ascii=False) if msg.tool_calls else "",
    ])


def tools_key(tools: Optional[List[Tool]]) -> str:
    if not tools:
        return ""
    payload = json.dumps([t.model_dump() for t in tools], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def conversation_key(request: ChatRequest, header_id: Optional[str]) -> str:
    """Identify one harness conversation across its successive full-history calls."""
    explicit = request.conversation_id or request.sessionId or header_id
    if explicit:
        return "cid:" + hashlib.sha256(explicit.encode()).hexdigest()[:24]
    system = next((get_content_text(m.content) for m in request.messages if m.role in ("system", "developer")), "")
    user = next((get_content_text(m.content) for m in request.messages if m.role == "user"), "")
    if not (system or user):
        user = " ".join(get_content_text(m.content) for m in request.messages)
    return "seed:" + hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:24]


def plan_delta(key: str, request: ChatRequest, tools: Optional[List[Tool]]):
    """Return (state, new messages) when a parked tab continues this conversation."""
    state = conversations.get(key)
    if not state:
        return None
    if state["session"] not in session_pages:
        conversations.pop(key, None)
        return None
    if state["tools"] != tools_key(tools):
        return None
    consumed = state["consumed"]
    if len(request.messages) <= consumed:
        return None
    if [message_key(m) for m in request.messages[:consumed]] != state["prefix"]:
        return None
    # The harness echoes the browser's own answer back to us; it must match.
    if message_key(request.messages[consumed]) != state["echo"]:
        return None
    delta = request.messages[consumed + 1:]
    if not delta:
        return None
    return state, delta


def delta_prompt(messages: List[Message], max_tokens: Optional[int] = None, hint: Optional[str] = None) -> str:
    prompt = (
        "Continue this same chat. Earlier turns are already here, so do not repeat or summarise them.\n\n"
        + format_conversation(messages)
    )
    if hint:
        prompt += f"\n\n{hint}"
    if max_tokens:
        prompt += f"\nKeep the answer within {max_tokens} output tokens."
    return prompt


FORCED_TOOL_INSTRUCTION = (
    "This turn requires a tool call. Reply with exactly one tool-call envelope for the most "
    "appropriate declared tool, and no additional prose."
)


def tool_choice_plan(choice: Any, tools: Optional[List[Tool]]):
    """Resolve an OpenAI tool_choice into advertised tools and a turn instruction.

    The browser chat has no native function calling, so a forced choice becomes an
    instruction inside the prompt instead of a rejected request.
    """
    if choice is None or choice == "auto":
        return tools, None
    if choice == "none":
        return None, "Do not call any tool this turn. Answer with plain text only."
    if choice in ("required", "any"):
        if not tools:
            raise HTTPException(400, "tool_choice requires declared tools")
        return tools, FORCED_TOOL_INSTRUCTION
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if not name:
            raise HTTPException(400, "Unsupported tool_choice value")
        if not tools or name not in {t.function.name for t in tools}:
            raise HTTPException(400, f"tool_choice names the undeclared tool '{name}'")
        return tools, f"This turn must call the tool '{name}' exactly once and nothing else."
    raise HTTPException(400, "Unsupported tool_choice value")


async def close_session(session_id: str) -> None:
    page = session_pages.pop(session_id, None)
    page_locks.pop(session_id, None)
    if page is not None:
        try:
            await page.close()
        except Exception:
            pass


async def evict_conversations() -> None:
    """Close idle or evicted conversation tabs so the browser stays small."""
    stale = [
        key for key, state in list(conversations.items())
        if state["session"] not in session_pages
        or time.monotonic() - state["touched"] > CONVERSATION_TTL
    ]
    for key in stale:
        session_id = conversations.pop(key, {}).get("session")
        if session_id:
            await close_session(session_id)
    while len(conversations) > MAX_PARKED_TABS:
        oldest = min(conversations, key=lambda item: conversations[item]["touched"])
        await close_session(conversations.pop(oldest)["session"])


async def browser_alive() -> bool:
    if context is None:
        return False
    try:
        await asyncio.wait_for(context.cookies("https://gemini.google.com"), timeout=5)
        return True
    except Exception:
        return False


async def stop_browser() -> None:
    global context, is_ready
    conversations.clear()
    for session_id in list(session_pages):
        await close_session(session_id)
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass
        context = None
    is_ready = False


async def ensure_ready() -> None:
    """Reconnect a dead browser session instead of failing every later request."""
    global is_ready, login_stage, login_error, playwright_instance
    if is_ready and await browser_alive():
        return
    if startup_task is not None and not startup_task.done():
        raise HTTPException(503, "Service initializing...")
    async with reconnect_lock:
        if is_ready and await browser_alive():
            return
        await stop_browser()
        if playwright_instance is None:
            playwright_instance = await async_playwright().start()
        if not LOGIN_FLAG.exists():
            login_stage = "manual-login"
            login_error = "Google 로그인이 만료되었습니다. 로그인 화면에서 다시 인증해 주세요."
            raise HTTPException(503, login_error)
        login_stage = "reconnecting"
        login_error = None
        print("Reconnecting the Gemini browser…", flush=True)
        try:
            await connect_context()
        except Exception:
            login_stage = "error"
            login_error = "Gemini 브라우저 자동 복구에 실패했습니다. 잠시 후 다시 시도해 주세요."
            raise HTTPException(503, login_error)


async def send_to_gemini(page: Page, text: str, timeout: Optional[int] = None) -> str:
    editor = page.locator("rich-textarea .ql-editor")
    await editor.wait_for(timeout=30000)
    await editor.fill(text)
    responses = page.locator('div[id^="model-response-message-content"]')
    before = await responses.count()
    await editor.press("Enter")
    deadline = time.monotonic() + min(timeout or 300, 600)
    previous, stable_since = "", time.monotonic()
    while time.monotonic() < deadline:
        if await responses.count() > before:
            answer = await responses.last.inner_text()
            if answer != previous:
                previous, stable_since = answer, time.monotonic()
            # Do not mistake a pause in a long answer for generation completion.
            generating = await page.locator('button[aria-label*="Stop"], button[aria-label*="중지"], button.stop-button').first.is_visible()
            if answer.strip() and not generating and time.monotonic() - stable_since >= 3:
                # Preserve inline JSON before Markdown conversion can escape
                # underscores and brackets in tool names or arguments.
                if parse_tool_calls(answer):
                    return answer.strip()
                # Gemini renders JSON in code widgets; toolbar text must not
                # become part of a tool argument envelope.
                for block in await responses.last.locator('pre code, pre').all():
                    candidate = await block.inner_text()
                    if parse_tool_calls(candidate):
                        return candidate.strip()
                html = await responses.last.inner_html()
                return md(html, heading_style="ATX", escape_underscores=False).strip()
        await asyncio.sleep(0.5)
    raise HTTPException(504, "Gemini browser response timed out")


def parse_tool_calls(response: str) -> Optional[List[Dict]]:
    cleaned = response.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        first_line, separator, remainder = cleaned.partition("\n")
        if separator and first_line.strip() in ("```", "```json"):
            cleaned = remainder[:-3].strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "tool_calls" not in data:
        return None
    calls = data["tool_calls"]
    if not isinstance(calls, list) or not calls:
        raise HTTPException(502, "Invalid Gemini tool-call envelope")
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), dict):
            raise HTTPException(502, "Invalid Gemini tool-call arguments")
    return calls


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest,
    x_conversation_id: Annotated[Optional[str], Header(alias="X-Conversation-Id")] = None,
):
    if not request.messages:
        raise HTTPException(400, "Messages required")
    
    if request.model != MODEL_ID and not request.model.startswith("gemini-web-"):
        raise HTTPException(404, "Unknown Gemini web model")
    if any(isinstance(m.content, list) and any(c.get("type") != "text" for c in m.content) for m in request.messages):
        raise HTTPException(400, "This browser route supports text input only")
    declared_tools = request.tools or None
    tools, tool_hint = tool_choice_plan(request.tool_choice, declared_tools)
    max_output_tokens = request.max_tokens or request.max_completion_tokens
    # The harness resends the full history each turn. Reuse the parked tab and
    # send only the new messages; rebuild from scratch whenever anything diverges.
    key = conversation_key(request, x_conversation_id)
    async with request_lock:
        await ensure_ready()
        await evict_conversations()
        resumed = plan_delta(key, request, declared_tools)
        if resumed:
            state, delta = resumed
            session_id = state["session"]
            page = session_pages[session_id]
            conversation = delta_prompt(delta, max_output_tokens, tool_hint)
        else:
            previous = conversations.pop(key, None)
            if previous:
                await close_session(previous["session"])
            state = None
            session_id = uuid.uuid4().hex
            page = await get_or_create_session_page(session_id)
            conversation = format_conversation(request.messages, tools, budget=HISTORY_CHAR_BUDGET)
            if tool_hint:
                conversation += f"\n\n{tool_hint}"
            if max_output_tokens:
                conversation += f"\nKeep the answer within {max_output_tokens} output tokens."
        try:
            if state is None or state.get("model") != request.model:
                await choose_mode(page, request.model)
            response = await send_to_gemini(page, conversation, request.timeout)
            print("Gemini response received.", flush=True)
            
            # Check for tool calls
            tool_calls = None
            finish_reason = "stop"
            
            if tools:
                parsed_tools = parse_tool_calls(response)
                if parsed_tools:
                    tool_calls = []
                    for i, tc in enumerate(parsed_tools):
                        if tc["name"] not in {t.function.name for t in tools}:
                            raise HTTPException(502, "Gemini requested an undeclared tool")
                        tool_calls.append({
                            "id": f"call_{int(time.time())}_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name"),
                                "arguments": json.dumps(tc.get("arguments", {}))
                            }
                        })
                    finish_reason = "tool_calls"
                    print(f"🔧 Tool calls detected: {[tc['function']['name'] for tc in tool_calls]}")
            
            # Build response message
            msg = {"role": "assistant"}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                msg["content"] = None
            else:
                msg["content"] = response
            
            conversations[key] = {
                "session": session_id,
                "prefix": [message_key(m) for m in request.messages],
                "consumed": len(request.messages),
                "echo": message_key(Message(**msg)),
                "tools": tools_key(declared_tools),
                "model": request.model,
                "touched": time.monotonic(),
            }
            
            result = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model or "gemini-pro",
                "choices": [{
                    "index": 0,
                    "message": msg,
                    "finish_reason": finish_reason
                }],
                "usage": {
                    "prompt_tokens": len(conversation) // 4,
                    "completion_tokens": len(response) // 4,
                    "total_tokens": (len(conversation) + len(response)) // 4
                }
            }
            
            # Handle streaming
            include_usage = bool((request.stream_options or {}).get("include_usage"))
            if request.stream:
                async def generate():
                    chunk_id = result["id"]
                    created = int(time.time())
                    model = request.model or "gemini-pro"
                    
                    if tool_calls:
                        # First chunk with role
                        first_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": None},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(first_chunk)}\n\n"
                        
                        # Stream each tool call
                        for i, tc in enumerate(tool_calls):
                            tc_chunk = {
                                "id": chunk_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [{
                                            "index": i,
                                            "id": tc["id"],
                                            "type": "function",
                                            "function": {
                                                "name": tc["function"]["name"],
                                                "arguments": tc["function"]["arguments"]
                                            }
                                        }]
                                    },
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(tc_chunk)}\n\n"
                    else:
                        # Stream content - first chunk with role
                        first_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(first_chunk)}\n\n"
                        
                        # Content chunk
                        content_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": response},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(content_chunk)}\n\n"
                    
                    # Final chunk with finish_reason
                    done_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason
                        }]
                    }
                    yield f"data: {json.dumps(done_chunk)}\n\n"
                    if include_usage:
                        usage_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [],
                            "usage": result["usage"],
                        }
                        yield f"data: {json.dumps(usage_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                
                return StreamingResponse(generate(), media_type="text/event-stream")
            
            return result
            
        except HTTPException:
            conversations.pop(key, None)
            await close_session(session_id)
            raise
        except Exception:
            conversations.pop(key, None)
            await close_session(session_id)
            if not await browser_alive():
                await stop_browser()
                raise HTTPException(503, "Gemini 브라우저 연결이 끊겨 자동 복구를 시작했습니다. 잠시 후 다시 시도해 주세요.")
            raise HTTPException(502, "Gemini browser request failed; check the subscription login page")


@app.get("/v1/models")
async def list_models():
    async with request_lock:
        await ensure_ready()
        page = await context.new_page()
        try:
            await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
            choices = await discover_modes(page)
            model_choices.clear()
            model_choices.update(choices)
        finally:
            await page.close()
    return {"object": "list", "data": [
        {"id": MODEL_ID, "name": "Gemini Web · 웹에서 현재 선택한 모드", "object": "model", "owned_by": "google-web"},
        *[{**choice, "object": "model", "owned_by": "google-web"} for choice in model_choices.values()],
    ]}


USAGE_CACHE_TTL = 60.0
_usage_cache = {"at": 0.0, "value": None}


@app.get("/v1/usage")
async def usage(fresh: int = 0):
    """Read the Gemini Apps usage page in the signed-in browser and return its text.

    The page costs a browser navigation, so the answer is shared for a minute
    unless the caller asks for `fresh=1`.
    """
    now = time.time()
    if not fresh and _usage_cache["value"] is not None and now - _usage_cache["at"] < USAGE_CACHE_TTL:
        return _usage_cache["value"]
    async with request_lock:
        await ensure_ready()
        page = await context.new_page()
        try:
            await page.goto("https://gemini.google.com/usage", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            url = page.url
            cookies = await page.context.cookies("https://gemini.google.com")
            account = any(c["name"] in ("SID", "__Secure-1PSID", "__Secure-3PSID") for c in cookies)
            signed_in = account and url.startswith("https://gemini.google.com/") and "accounts.google.com" not in url
            title = await page.title()
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        finally:
            await page.close()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    value = {
        "signedIn": signed_in,
        "url": url,
        "title": title,
        "lines": lines[:200],
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _usage_cache["at"] = time.time()
    _usage_cache["value"] = value
    return value


@app.get("/health")
async def health():
    return {
        "status": "ready" if is_ready else "initializing",
        "stage": login_stage,
        "error": login_error,
        "sessions": len(session_pages),
        "conversations": [
            {"session": state["session"], "messages": state["consumed"]}
            for state in conversations.values()
        ],
    }


@app.post("/login/complete")
async def login_complete():
    if manual_browser is None or manual_browser.returncode is not None:
        raise HTTPException(409, "먼저 Google 로그인 화면을 열어 주세요.")
    LOGIN_COMPLETE.write_text("ok")
    return {"submitted": True}


@app.post("/reset")
async def reset():
    LOGIN_FLAG.unlink(missing_ok=True)
    return {"status": "Login cleared. Restart to login again."}


@app.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str):
    if session_id in session_pages:
        for key, state in list(conversations.items()):
            if state["session"] == session_id:
                conversations.pop(key, None)
        await close_session(session_id)
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(404, "Session not found")


@app.get("/v1/sessions")
async def list_sessions():
    return {"sessions": list(session_pages.keys()), "count": len(session_pages)}


@app.on_event("startup")
async def startup():
    print("\n" + "="*50)
    print("  00BX GEMINI API SERVICE")
    print("  OpenAI-Compatible API for Gemini Web")
    print("="*50)
    global startup_task, cleanup_task
    async def initialize():
        global login_stage, login_error, is_ready
        try:
            await init_browser()
        except asyncio.CancelledError:
            raise
        except Exception:
            is_ready = False
            login_stage = "error"
            login_error = "Gemini 브라우저 연결을 완료하지 못했습니다. 로그인 화면에서 다시 시도해 주세요."
    startup_task = asyncio.create_task(initialize())

    async def janitor():
        while True:
            await asyncio.sleep(60)
            if request_lock.locked():
                continue
            async with request_lock:
                try:
                    await evict_conversations()
                except Exception:
                    pass
    cleanup_task = asyncio.create_task(janitor())


@app.on_event("shutdown")
async def shutdown():
    global context, playwright_instance, manual_browser, cleanup_task
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
    conversations.clear()
    if manual_browser and manual_browser.returncode is None:
        manual_browser.terminate()
        await manual_browser.wait()
    if startup_task and not startup_task.done():
        startup_task.cancel()
        try:
            await startup_task
        except asyncio.CancelledError:
            pass
    for page in session_pages.values():
        try:
            await page.close()
        except:
            pass
    if context:
        await context.close()
    if playwright_instance:
        await playwright_instance.stop()
