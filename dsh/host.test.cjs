const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Readable} = require('node:stream');
const {callbackPath,createHandler} = require('./host.cjs');
test('only the fixed Codex callback can be relayed',()=>{
 assert.match(callbackPath('http://localhost:1455/auth/callback?code=c&state=s'), /^\/auth\/callback/);
 for (const url of ['https://evil.test/?code=c&state=s','http://localhost:8080/auth/callback?code=c&state=s','http://localhost:1455/auth/callback?code=c','http://user:pass@localhost:1455/auth/callback?code=c&state=s']) assert.throws(()=>callbackPath(url));
});
async function invoke(headers,url,body,fetch,codexStatus=async()=>({connected:true,pending:false})){
 const req=Readable.from([Buffer.from(JSON.stringify(body))]);Object.assign(req,{method:'POST',url,headers});
 const res={writeHead(status){this.status=status},end(data){this.body=JSON.parse(data)}};
 await createHandler({fetch,codexStatus})(req,res);return res;
}
test('cross-origin callbacks cannot trigger an exchange',async()=>{
 let called=false;const res=await invoke({host:'ai.test',origin:'https://evil.test','x-subscription-action':'1'},'/subscription-login/codex/complete',{url:'http://localhost:1455/auth/callback?code=c&state=s'},async()=>{called=true});
 assert.equal(res.status,403);assert.equal(called,false);
});
test('callback exchange is fixed to loopback and never returns a code',async()=>{
 let target;const res=await invoke({host:'ai.test',origin:'https://ai.test','x-subscription-action':'1'},'/subscription-login/codex/complete',{url:'http://localhost:1455/auth/callback?code=private&state=s'},async url=>{target=url;return {ok:true}});
 assert.equal(target,'http://127.0.0.1:1455/auth/callback?code=private&state=s');assert.deepEqual(res.body,{connected:true});
});

test('a successful callback HTTP response is not treated as a saved grant',async()=>{
 const res=await invoke({host:'ai.test',origin:'https://ai.test','x-subscription-action':'1'},'/subscription-login/codex/complete',{url:'http://localhost:1455/auth/callback?code=private&state=s'},async()=>({ok:true}),async()=>({connected:false,pending:false,error:'Token exchange failed'}));
 assert.equal(res.status,409);assert.equal(res.body.detail,'Token exchange failed');assert.equal(JSON.stringify(res.body).includes('private'),false);
});
test('pending token exchange is awaited before returning success',async()=>{
 let calls=0;const res=await invoke({host:'ai.test',origin:'https://ai.test','x-subscription-action':'1'},'/subscription-login/codex/complete',{url:'http://localhost:1455/auth/callback?code=private&state=s'},async()=>({ok:true}),async()=>++calls===1?{connected:false,pending:true}:{connected:true,pending:false});
 assert.equal(calls,2);assert.equal(res.status,200);assert.deepEqual(res.body,{connected:true});
});
