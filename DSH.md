# DSH subscription login integration

This fork adds a DSH plugin and deployment files to
[zemenbeg/gemini-web-proxy](https://github.com/zemenbeg/gemini-web-proxy).
It uses an existing Gemini Web account rather than a paid Gemini API key.
Codex requests are handled by the separately installed
[dsh-subscriptions](https://github.com/DevViking-Persike/dsh-subscriptions)
plugin; this fork supplies its browser login interface.

## Login from DSH

Install this repository as a DSH bundle, restart DSH, and open
**Plugins → dsh-subscription-login**. The Codex subscription plugin's page
also includes the panel. A direct link is `/subscription-login/`.

- **Codex:** choose **ChatGPT로 로그인**, finish the vendor's consent flow,
  and copy the entire `http://localhost:1455/auth/callback?...` URL from the
  resulting tab into the panel. A connection error on that localhost page is
  expected when DSH runs on another machine. The panel submits the URL in a
  POST body to the server's fixed loopback callback; the existing OAuth flow
  validates its state and exchanges its code. The panel waits for the grant to
  be stored before reporting success. Failed submissions keep the pasted URL;
  progress and errors appear beside the affected provider. No SSH forwarding is required.
- **Gemini:** choose **Google 로그인 화면 열기**, sign in through the dedicated
  ordinary Chrome browser, and select the desired Gemini mode. Authentication
  runs without Playwright, WebDriver, or a browser debugging endpoint. Return to the panel and choose
  **Gemini 연결 완료**. The browser then switches to headless mode. Open the
  browser again to renew login or change modes.

Both panels show current connection status. Model inference has not been
verified until a real account login and model request succeed.

After connecting Gemini, the panel reads the account's actual model menu and
updates the existing DSH `gemini-web` provider through DSH's Settings service.
**웹 모델 목록 새로고침** repeats discovery without a DSH restart. Only enabled
model entries are included; thinking-depth controls are not models. DSH shows
the menu's labels, and request ids use the web menu's stable mode identities.
Choosing one in DSH selects and verifies that mode in the browser before
sending the request. A removed/unavailable mode fails explicitly.

The background browser uses the same operating-system credential store as
ordinary Chrome. Playwright's default `--password-store=basic` override is
disabled so it can decrypt the manual login's cookies. A failed session check
reports an error and stops; it never automatically cycles through login windows.

## Host deployment

The files in `deploy/` are templates for the current shortstories host. Adjust
their paths and service user before deploying to a different installation.
The bridge requires Chrome, Python dependencies from `requirements.txt`, Xvfb,
x11vnc, websockify, and noVNC. The provided service uses the existing host
Chrome installation and does not install a second browser.

1. Keep the checkout under `plugins/gemini-web-proxy`, create
   `runtime/gemini-web-proxy-env`, and install `requirements.txt` there.
2. Copy the service and startup script from `deploy/` to their configured
   locations, then enable `dsh-gemini-web.service`.
3. Generate a private random proxy token at
   `$DSH_HOME/integrations/gemini-web/proxy.token` (mode 0600). Save the same
   token as `GEMINI_WEB_PROXY_TOKEN` in the DSH credential store. This token
   protects the local bridge; it is not a Google API key.
4. Include `deploy/nginx-subscription-browser.conf` in the existing authenticated
   DSH nginx server block. Preserve its PAM/authentication gate for HTTP and
   WebSocket requests. Do not expose ports 15901 or 16080 directly.
5. Install `dsh-subscriptions` and this repository with `dsh plugin --profile web
   add github:<owner>/gemini-web-proxy#<commit>`. Merge the provider settings in
   `deploy/cordis.example.yml` into the existing profile. Model selections use
   `codex-oauth` for Codex and `gemini-web/gemini-web` for Gemini.

The login button invokes the fixed `sudo -n systemctl restart dsh-gemini-web`
command. The DSH service user must have permission to run that specific command.
Login uses display :99. The API binds 127.0.0.1:18089, VNC binds loopback port
15901, and noVNC binds 127.0.0.1:16080. Cookie/profile state belongs under
`$DSH_HOME/integrations/gemini-web`, outside this repository.

## Bridge behavior and limits

DSH resends the whole message history on every turn. The bridge identifies the
conversation (`conversation_id`, `sessionId`, or the `X-Conversation-Id` header,
falling back to a fingerprint of the system prompt and first user message) and
keeps that conversation's Gemini chat open: the first turn sends the full
prompt, later turns send only the new messages, so earlier turns are neither
duplicated in the browser nor lost from its context. A tab is rebuilt and the
full history is resent whenever the history diverges, the declared tools change,
or the browser died; long rebuilt prompts clip older turns to a fixed budget
while the newest turns stay verbatim. Parked conversations close after
`CONVERSATION_TTL` seconds or when more than `MAX_PARKED_TABS` exist. A shared
lock still serializes requests so independent DSH sessions never interleave
inside the browser. Gemini Web may retain the created chats in the account's
own activity history.

The model id `gemini-web` denotes the web application's currently selected
mode; it does not promise a particular backend model. Discovered
`gemini-web-<web-mode-id>` ids explicitly select their corresponding menu entry.
The visible mode labels come from the account rather than a hard-coded model
catalog. Input is text only.
Tool calls use JSON generated from the supplied definitions, with declared
names and object arguments checked before dispatch. This is experimental and
does not offer native function-calling guarantees. Browser UI changes can
require selector updates.

Streaming is buffered: SSE chunks are emitted after the browser completes.
Usage is estimated from character counts and included as a final chunk when the
client asks for `stream_options.include_usage`, the way the OpenAI SDK does.
`max_tokens` and `max_completion_tokens` are prompt guidance, not a
provider-enforced token cap. The 32K context configuration is a conservative
local admission setting, not a verified Google account limit.
Forced tool choice (`required`, `any`, or a named function) becomes an
instruction inside the prompt instead of a rejected request, while image input
fails explicitly. The bridge does not return reasoning or provider thought
signatures.

`GET /health` reports `stage` (`connected`, `reconnecting`, `manual-login`),
the last error, and the parked conversations. A dropped Chromium context is
relaunched and re-verified before the request is served; only an expired Google
session falls back to the manual login flow.

## Validation

```sh
PYTHONPATH=. python -m unittest discover -s tests -v
node --test dsh/host.test.cjs
# With playwright-core available, use Chrome to verify login UI state changes:
node tests/login-ui.mjs
```

Tests cover tab reuse between turns, rebuilt tabs on divergence, history
compaction, dead-browser recovery, tool-call SSE serialization, usage chunks,
nested JSON arguments, undeclared tools, forced tool choice, image rejection,
callback destination checks, and cross-origin callback rejection. Real
Google/OpenAI login and inference remain separate live checks.
