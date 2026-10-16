import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, rm, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMemoryServer } from '../backend/server.mjs';
import { ExistingCodeGraph } from '../bridge/existing-graph.mjs';

const cli = fileURLToPath(new URL('../bin/memory-lens.mjs', import.meta.url));
const credential = 'synthetic-integration-only-credential';
const op = (kind, fields) => ({op:kind,client_id:null,node_id:null,type:null,content:null,source_node_id:null,target_node_id:null,label:null,evidence:null,evidence_origin:null,...fields});

function hook(event, input, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli,'hook',event], {env,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='';
    child.stdout.on('data', chunk => stdout+=chunk);
    child.stderr.on('data', chunk => stderr+=chunk);
    child.on('error',reject);
    child.on('close',code => {
      try { assert.equal(code,0,stderr); resolve(JSON.parse(stdout)); } catch(error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(input));
  });
}
function cliRequest(args, input, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => stdout += chunk);
    child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', reject);
    child.on('close', code => { try { assert.equal(code, 0, stderr + stdout); resolve(JSON.parse(stdout)); } catch (error) { reject(error); } });
    child.stdin.end(input ? JSON.stringify(input) : '');
  });
}

test('user prompt memory edits reach the next agent turn before existing KG retrieval, without assistant capture', async t => {
  const directory=await mkdtemp(join(tmpdir(),'memory-lens-integration-'));
  const responses=[], providerBodies=[];
  const codeNode={qualified_name:'/synthetic/refunds.py::validate_refund',kind:'Function',name:'validate_refund',file_path:'/synthetic/refunds.py',line_start:10,line_end:20};
  const fixtureCode=join(directory,'code-graph-fixture.mjs'), codeLog=join(directory,'code-requests.jsonl');
  await writeFile(fixtureCode, `
import {createInterface} from 'node:readline';
import {appendFileSync} from 'node:fs';
const node=JSON.parse(process.argv[2]), log=process.argv[3];
for await(const line of createInterface({input:process.stdin})){
  const request=JSON.parse(line);
  appendFileSync(log,JSON.stringify(request)+'\\n');
  if(!Object.hasOwn(request,'id'))continue;
  const value=request.method==='initialize'?{protocolVersion:'2025-06-18',capabilities:{tools:{}},serverInfo:{name:'synthetic-existing-code-graph',version:'1'}}:
    request.params?.name==='get_minimal_context_tool'?{status:'ok',summary:'Synthetic indexed graph'}:
    request.params?.name==='semantic_search_nodes_tool'?{status:'ok',results:[node]}:{status:'ok',results:[],edges:[]};
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request.id,result:request.method==='initialize'?value:{structuredContent:value,content:[{type:'text',text:JSON.stringify(value)}]}})+'\\n');
}
`);
  const codeSource=await readFile(fixtureCode,'utf8');
  const codeGraph=new ExistingCodeGraph({env:{...process.env,MEMORY_LENS_CODE_GRAPH_REPO:directory,MEMORY_LENS_CODE_GRAPH_COMMAND_JSON:JSON.stringify([process.execPath,fixtureCode,JSON.stringify(codeNode),codeLog])}});
  const provider=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;providerBodies.push(JSON.parse(body));
    const operations=responses.shift()??[];
    res.writeHead(200,{'content-type':'application/json'});
    res.end(JSON.stringify({status:'completed',output:[{type:'message',content:[{type:'output_text',text:JSON.stringify({operations,message:'Synthetic user-prompt extraction'})}]}]}));
  });
  await new Promise(resolve=>provider.listen(0,'127.0.0.1',resolve));
  const app=await startMemoryServer({token:credential,apiKey:'synthetic-provider-key',dbPath:join(directory,'memory.sqlite'),providerUrl:`http://127.0.0.1:${provider.address().port}/v1/responses`,codeGraph});
  t.after(async()=>{await app.close();provider.closeAllConnections();await new Promise(resolve=>provider.close(resolve));await rm(directory,{recursive:true,force:true});});
  const env={...process.env,MEMORY_LENS_API_URL:app.url,MEMORY_LENS_TOKEN:credential,MEMORY_LENS_STATE_DIR:join(directory,'state'),MEMORY_LENS_TIMEOUT_MS:'5000'};
  const api=async body=>{
    const r=await fetch(app.url,{method:'POST',headers:{'content-type':'application/json',authorization:`Bearer ${credential}`},body:JSON.stringify(body)});
    assert.equal(r.status,200);return r.json();
  };
  responses.push([
    op('add',{client_id:'english',type:'constraint',content:'Reply in British English',evidence:'use British English',evidence_origin:'user'}),
    op('add',{client_id:'dash',type:'constraint',content:'Use em dashes',evidence:'em dashes',evidence_origin:'user'}),
  ]);
  const first=await hook('UserPromptSubmit',{session_id:'billing-chat',turn_id:'first',prompt:'Implement refund validation. When replying, use British English and em dashes.'},env);
  const sessions=(await api({action:'list_sessions'})).sessions;
  assert.equal(sessions.length,1);
  const session=sessions[0];
  assert.ok(session);
  const before=await api({action:'graph',session_id:session.id});
  assert.equal(before.nodes.filter(n=>n.status==='active').length,2,'memories must exist before any assistant response or Stop');
  assert.match(first.hookSpecificOutput.additionalContext,/British English/);
  assert.match(first.hookSpecificOutput.additionalContext,/validate_refund/);
  assert.equal(before.latest_retrieval.code_graph.status,'ready');
  assert.ok(before.nodes.every(n=>n.id!==codeNode.qualified_name),'code nodes are never merged into chat memory');
  await hook('Stop',{session_id:'billing-chat',turn_id:'first',last_assistant_message:'ASSISTANT-ONLY-SECRET: use American English and remove British English.'},env);
  assert.equal(providerBodies.length,1,'assistant output never invokes memory extraction');
  assert.doesNotMatch(JSON.stringify(providerBodies),/ASSISTANT-ONLY-SECRET|American English/);
  assert.ok(before.turns.every(turn=>!turn.assistant_message),'captured turns contain no assistant text');

  const dash=before.nodes.find(node=>node.content==='Use em dashes');
  responses.push([op('forget',{node_id:dash.id,evidence:'Forget the em-dash instruction',evidence_origin:'command'})]);
  await api({action:'command',session_id:session.id,request_id:'lovable-forget',command:'Forget the em-dash instruction'});
  const edited=await api({action:'graph',session_id:session.id});
  assert.equal(edited.nodes.find(node=>node.id===dash.id).status,'forgotten');
  assert.equal(edited.latest_retrieval,undefined,'old retrieval trace clears after a memory edit');
  const next=await hook('UserPromptSubmit',{session_id:'billing-chat',turn_id:'next',prompt:'Continue'},env);
  assert.match(next.hookSpecificOutput.additionalContext,/British English/);
  assert.doesNotMatch(next.hookSpecificOutput.additionalContext,/Use em dashes/);
  assert.match(next.hookSpecificOutput.additionalContext,/validate_refund/);
  const final=await api({action:'graph',session_id:session.id});
  assert.deepEqual(final.latest_retrieval.code_graph.graph,before.latest_retrieval.code_graph.graph,'memory edit never rebuilds/changes the existing code graph');
  const requests=(await readFile(codeLog,'utf8')).trim().split('\n').map(line=>JSON.parse(line));
  const lastSearch=requests.filter(request=>request.params?.name==='semantic_search_nodes_tool').at(-1);
  assert.match(lastSearch.params.arguments.query,/British English/);
  assert.doesNotMatch(lastSearch.params.arguments.query,/Use em dashes/);
  assert.doesNotMatch(JSON.stringify(requests),/build_or_update_graph|embed_graph|watch_tool/);
  assert.equal(await readFile(fixtureCode,'utf8'),codeSource);
  const long=await hook('UserPromptSubmit',{session_id:'billing-chat',turn_id:'long',prompt:'Continue with this detailed task: '+'x'.repeat(700)},env);
  assert.match(long.hookSpecificOutput.additionalContext,/British English/);
  responses.push([op('add',{client_id:'deploy',type:'constraint',content:'Deploy on Fridays',evidence:'Deploy on Fridays',evidence_origin:'user'})]);
  const other=await hook('UserPromptSubmit',{session_id:'deployment-chat',turn_id:'first',prompt:'Fix deployment. Deploy on Fridays.'},env);
  assert.match(other.hookSpecificOutput.additionalContext,/British English|Deploy on Fridays/);
  assert.doesNotMatch(other.hookSpecificOutput.additionalContext,/Use em dashes/);
  const shared=await api({action:'graph',session_id:session.id});
  assert.equal(shared.nodes.filter(n=>n.status==='active').length,2,'the same shared graph grows from another native chat');
  assert.ok(shared.nodes.some(node=>node.content==='Deploy on Fridays' && node.external_session_id==='deployment-chat'));
  assert.ok(shared.nodes.some(node=>node.content==='Reply in British English' && node.external_session_id==='billing-chat'));
  const callsBeforeDuplicate=providerBodies.length;
  await hook('UserPromptSubmit',{session_id:'deployment-chat',turn_id:'first',prompt:'Fix deployment. Deploy on Fridays.'},env);
  assert.equal(providerBodies.length,callsBeforeDuplicate,'same-origin duplicate hook never rebills');
  const editorContext=await cliRequest(['context','--source','editor','--session','editor-native','--query','Continue'],null,env);
  assert.equal(editorContext.session_id,session.id);
  assert.match(editorContext.text,/British English|Deploy on Fridays/);
  assert.doesNotMatch(editorContext.text,/Use em dashes/);
  assert.ok(editorContext.node_ids.includes(before.nodes.find(node=>node.content==='Reply in British English').id),'another tool retrieves the same preference node');
  const event={source:'editor',external_session_id:'editor-native',turn_id:'first',user_message:'Continue with the shared preferences'};
  const ingested=await cliRequest(['ingest'],event,env);
  assert.equal(ingested.status,'noop');
  const callsBeforeRetry=providerBodies.length;
  const retried=await cliRequest(['ingest'],event,env);
  assert.equal(retried.status,'duplicate');
  assert.equal(providerBodies.length,callsBeforeRetry,'same-origin CLI retries never rebill');
  const finalShared=await api({action:'graph',session_id:session.id});
  assert.ok(finalShared.turns.filter(turn=>turn.native_turn_id==='first').length>=3,'colliding native IDs from distinct tool/chat origins remain distinct captures');
  assert.equal((await api({action:'list_sessions'})).sessions.length,1);
});
