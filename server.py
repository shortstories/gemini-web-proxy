from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Union
import json
import asyncio
import time
import re
import os
import uuid
import hmac
import shutil
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
startup_task = None
manual_browser = None
login_stage = "starting"
MODEL_ID = "gemini-web"

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
    model: Optional[str] = "gemini-web"
    messages: List[Message]
    stream: Optional[bool] = False
    timeout: Optional[int] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None

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
    global context, playwright_instance, is_ready, manual_browser, login_stage
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
        context = await playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=True, channel="chrome",
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
            continue
        LOGIN_FLAG.write_text("ok")
        await page.close()
        is_ready = True
        login_stage = "connected"
        print("Gemini web subscription is ready (headless).", flush=True)
        return


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


def format_conversation(messages: List[Message], tools: Optional[List[Tool]] = None) -> str:
    """Format messages into a conversation prompt"""
    formatted = []
    
    # Add tools prompt at the beginning if tools are provided
    if tools:
        formatted.append(format_tools_prompt(tools))
    
    for msg in messages:
        role = msg.role
        content = get_content_text(msg.content)
        
        if role in ("system", "developer"):
            formatted.append(f"System Instructions:\n{content}")
        elif role == "user":
            formatted.append(f"User: {content}")
        elif role == "assistant":
            if msg.tool_calls:
                # Format previous tool calls
                tc_str = json.dumps({"tool_calls": [
                    {"name": tc.get("function", {}).get("name"), 
                     "arguments": json.loads(tc.get("function", {}).get("arguments", "{}"))}
                    for tc in msg.tool_calls
                ]})
                formatted.append(f"Assistant: {tc_str}")
            elif content:
                formatted.append(f"Assistant: {content}")
        elif role == "tool":
            tool_name = msg.name or "tool"
            formatted.append(f"Tool Result ({tool_name}, call_id={msg.tool_call_id}):\n{content}")
    
    return "\n\n".join(formatted)


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
                # Gemini renders JSON in code widgets; toolbar text must not
                # become part of a tool argument envelope.
                for block in await responses.last.locator('pre code, pre').all():
                    candidate = await block.inner_text()
                    if parse_tool_calls(candidate):
                        return candidate.strip()
                html = await responses.last.inner_html()
                return md(html, heading_style="ATX").strip()
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
async def chat_completions(request: ChatRequest):
    if not is_ready:
        raise HTTPException(503, "Service initializing...")
    
    if not request.messages:
        raise HTTPException(400, "Messages required")
    
    if request.model != MODEL_ID:
        raise HTTPException(404, "Unknown model; use gemini-web")
    if any(isinstance(m.content, list) and any(c.get("type") != "text" for c in m.content) for m in request.messages):
        raise HTTPException(400, "This browser route supports text input only")
    if request.tool_choice not in (None, "auto", "none"):
        raise HTTPException(400, "Forced tool choice is not supported")
    tools = request.tools if request.tool_choice != "none" else None
    # Each call supplies the full harness history. A fresh tab avoids mixing
    # independent DSH sessions or duplicating earlier turns in browser history.
    async with request_lock:
        session_id = uuid.uuid4().hex
        page = await get_or_create_session_page(session_id)
        conversation = format_conversation(request.messages, tools)
        if request.max_tokens:
            conversation += f"\nKeep the answer within {request.max_tokens} output tokens."
        try:
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
                    yield "data: [DONE]\n\n"
                
                return StreamingResponse(generate(), media_type="text/event-stream")
            
            return result
            
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Gemini browser request failed; check the subscription login page")
        finally:
            await delete_session(session_id)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_ID, "object": "model", "created": int(time.time()), "owned_by": "google-web"},
        ]
    }


@app.get("/health")
async def health():
    return {"status": "ready" if is_ready else "initializing", "stage": login_stage, "sessions": len(session_pages)}


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
        try:
            await session_pages[session_id].close()
        except:
            pass
        del session_pages[session_id]
        if session_id in page_locks:
            del page_locks[session_id]
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
    global startup_task
    startup_task = asyncio.create_task(init_browser())


@app.on_event("shutdown")
async def shutdown():
    global context, playwright_instance, manual_browser
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
