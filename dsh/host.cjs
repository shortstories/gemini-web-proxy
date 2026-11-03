const {readFile,writeFile,mkdir,unlink} = require('node:fs/promises');
const {join} = require('node:path');
const {homedir} = require('node:os');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const run = promisify(execFile);
const ROOT = '/subscription-login';
const JSON_HEADERS = {'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store'};

function callbackPath(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname)
      || url.port !== '1455' || url.pathname !== '/auth/callback' || url.username || url.password
      || !url.searchParams.get('state') || (!url.searchParams.get('code') && !url.searchParams.get('error'))) {
    throw new Error('인증 후 표시된 http://localhost:1455/auth/callback 주소 전체를 붙여 넣어 주세요.');
  }
  return url.pathname + url.search;
}

async function readBody(req) {
  const chunks = []; let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16384) throw new Error('입력이 너무 깁니다.');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

async function jsonFetch(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error('연결 상태를 확인할 수 없습니다.');
  return response.json();
}

function createHandler(deps = {}) {
  const localFetch = deps.fetch || fetch;
  const codexStatus = deps.codexStatus || (() => jsonFetch('http://127.0.0.1:1458/codex/status'));
  let pendingCodexUrl;
  const state = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'integrations/gemini-web');
  const service = deps.service || (async () => {
    await mkdir(state, {recursive: true, mode: 0o700});
    await unlink(join(state, 'login-complete')).catch(error => {if (error.code !== 'ENOENT') throw error;});
    await writeFile(join(state, 'interactive-request'), 'ok', {mode: 0o600});
    await run('/usr/bin/sudo', ['-n', '/usr/bin/systemctl', 'restart', 'dsh-gemini-web'], {timeout: 20000});
    for (let attempt = 0; attempt < 40; attempt++) {
      try {
        await jsonFetch('http://127.0.0.1:18089/health');
        const response = await localFetch('http://127.0.0.1:16080/vnc.html', {signal: AbortSignal.timeout(1000)});
        if (response.ok) return;
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    throw new Error('로그인 브라우저가 시작되지 않았습니다.');
  });
  return async (req, res) => {
    const reply = (status, data) => {res.writeHead(status, JSON_HEADERS); res.end(JSON.stringify(data));};
    try {
      const path = new URL(req.url, 'http://localhost').pathname;
      if (req.method === 'GET' && [ROOT, ROOT + '/'].includes(path)) {
        res.writeHead(200, {'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
          'content-security-policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'"});
        res.end(await readFile(__dirname + '/page.html')); return;
      }
      if (req.method === 'GET' && path === ROOT + '/status') {
        const [codex, gemini] = await Promise.allSettled([
          codexStatus(), jsonFetch('http://127.0.0.1:18089/health'),
        ]);
        reply(200, {
          codex: codex.status === 'fulfilled' ? codex.value : {connected: false, unavailable: true},
          gemini: gemini.status === 'fulfilled' ? gemini.value : {status: 'stopped'},
        }); return;
      }
      if (req.method !== 'POST') {reply(404, {error: 'Not found'}); return;}
      // Authentication is supplied by the existing nginx PAM gate. Mutations
      // additionally reject cross-origin forms, even with cached Basic auth.
      const origin = req.headers.origin;
      if (!origin || new URL(origin).host !== req.headers.host || req.headers['x-subscription-action'] !== '1') {
        reply(403, {error: '이 dsh의 구독 로그인 화면에서 다시 시도해 주세요.'}); return;
      }
      if (path === ROOT + '/codex/start') {
        const status = await codexStatus();
        if (status.pending && pendingCodexUrl) {reply(200, {url: pendingCodexUrl}); return;}
        const response = await localFetch('http://127.0.0.1:1458/codex/start', {redirect: 'manual', signal: AbortSignal.timeout(5000)});
        const location = response.headers.get('location');
        if (response.status !== 302 || !location || new URL(location).origin !== 'https://auth.openai.com') {
          throw new Error('진행 중인 인증 창을 먼저 완료하거나 닫은 뒤 다시 시도해 주세요.');
        }
        pendingCodexUrl = location;
        reply(200, {url: location}); return;
      }
      if (path === ROOT + '/codex/complete') {
        const body = await readBody(req);
        const path = callbackPath(body.url);
        const response = await localFetch('http://127.0.0.1:1455' + path, {signal: AbortSignal.timeout(15000), redirect: 'error'});
        if (!response.ok) throw new Error('인증 주소가 일치하지 않습니다. 가장 최근 로그인 창의 주소를 사용해 주세요.');
        const deadline = Date.now() + 30000;
        while (Date.now() < deadline) {
          const status = await codexStatus();
          if (!status.pending) {
            if (status.connected && !status.expired) {reply(200, {connected: true}); return;}
            reply(409, {error: 'ChatGPT 인증에 실패했습니다. 새 로그인부터 다시 시도해 주세요.', detail: status.error || '인증 흐름이 종료되었지만 연결 정보가 저장되지 않았습니다.'}); return;
          }
          await new Promise(resolve => setTimeout(resolve, 300));
        }
        reply(504, {error: 'ChatGPT 인증 확인 시간이 초과됐습니다. 입력한 주소는 유지됩니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.'}); return;
      }
      if (path === ROOT + '/gemini/start') {
        await service(); reply(200, {url: '/subscription-browser/vnc.html?autoconnect=true&resize=scale&path=subscription-browser/websockify'}); return;
      }
      if (path === ROOT + '/gemini/complete') {
        const token = (await readFile(join(state, 'proxy.token'), 'utf8')).trim();
        const response = await localFetch('http://127.0.0.1:18089/login/complete', {
          method: 'POST', headers: {authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error('먼저 Google 계정으로 로그인해 주세요.');
        reply(200, {submitted: true}); return;
      }
      reply(404, {error: 'Not found'});
    } catch (error) {
      // Do not echo callback URLs, authorization codes, or upstream bodies.
      const safe = /인증|입력|진행 중|로그인/.test(error.message) ? error.message : '연결하지 못했습니다. 잠시 후 다시 시도해 주세요.';
      reply(400, {error: safe});
    }
  };
}

exports.name = 'subscription-login';
exports.inject = ['webServer'];
exports.apply = (ctx) => ctx.effect(() => ctx.webServer.register({
  kind: 'prefix', path: ROOT, handler: createHandler(),
}), 'subscription login pages');
exports.callbackPath = callbackPath;
exports.createHandler = createHandler;
