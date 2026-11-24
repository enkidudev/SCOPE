/* Functional screen-state checks with a small DOM model, not a browser renderer.
   Run: node tests/test_dashboard.js. No third-party packages are required. */
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'dashboard.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'dashboard.js'), 'utf8');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.handlers={}; this._value=''; this.disabled=false; this.checked=false; this.textContent=''; }
  get value() { return this.tag==='select'?(this._value||this.children[0]?.value||''):this._value; }
  set value(value) { this._value=String(value); }
  get options() { return this.children; }
  append(...items) { this.children.push(...items); }
  add(option) { this.append(option); }
  replaceChildren(...items) { this.children=items; this._value=''; }
  setAttribute(key,value) { this[key]=value; }
  removeAttribute(key) { delete this[key]; }
  addEventListener(name,handler) { this.handlers[name]=handler; }
  querySelectorAll(selector) {
    const all=this.children.flatMap(item=>item instanceof Element?[item,...item.querySelectorAll('input')]:[]);
    return all.filter(item=>item.tag==='input'&&(selector!=='input:checked'||item.checked));
  }
}
const elements = {};
for(const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const item=new Element(match[1]);item.id=match[2];elements[item.id]=item;
}
for(const [id,value] of [['mode','records'],['filters','{}'],['scenario','repetitive'],['codec','none']]) elements[id].value=value;
const nav=['overview','ingest','explorer','investigation','benchmark'].map(view=>{const item=new Element('button');item.dataset.view=view;return item;});
const document={getElementById:id=>elements[id],createElement:tag=>new Element(tag),createTextNode:text=>text,
  querySelectorAll:selector=>selector==='nav button'?nav:selector==='.panel'?nav.map(item=>elements[item.dataset.view]):[]};
function Option(text,value) { const item=new Element('option');item.textContent=text;item.value=value;return item; }
const old={id:1,level:'F1',codec:'none',source_event_count:150,encoded_document_bytes:42,candidates:[{level:'F0',encoded_bytes:100,allowed:true,admissible:true,failed_contracts:[]},{level:'F1',encoded_bytes:42,allowed:true,admissible:true,failed_contracts:[]}],policy_version:'old-policy',sources:['old-host'],contracts:{OLD:{name:'Historical capability',fields:['principal']}},descriptor:{OLD:{status:'EXACT'}},field_coverage:{raw_payload:'UNAVAILABLE'}};
let currentPolicy={policy_version:'new-policy',contracts:{NEW:{name:'New capability',fields:['new_field']}}};
let segments=[old], deferQuery=null;
let guidedBody=null, answerRequest=null;
const benchmark=JSON.parse(fs.readFileSync(path.join(root,'data','benchmark.json'),'utf8'));
const fetch=async (url,options)=>({ok:true,json:async()=>{
  if(url==='/api/demo'){guidedBody=JSON.parse(options.body);const demo={...old,id:2,source_event_count:24,contracts:currentPolicy.contracts,descriptor:Object.fromEntries(Object.keys(currentPolicy.contracts).map(id=>[id,{status:'EXACT'}]))};segments=[demo];return demo;}
  if(url.startsWith('/api/state'))return {version:'0.2.2',policy:currentPolicy,segments,compression:{batches:segments.length,full_bytes:segments.length*100,retained_bytes:segments.length*42},stats:{segments:segments.length,input_bytes:100,encoded_document_bytes:42,candidate_audit_bytes:20,query_summary_bytes:0,sqlite_allocated_bytes:16384,queries:0}};
  if(url==='/api/benchmark')return benchmark;
  if(url==='/api/delete'){
    const {segment_id}=JSON.parse(options.body);
    segments=segments.filter(record=>record.id!==segment_id);
    return {deleted:segment_id};
  }
  if(url==='/api/query'){
    if(deferQuery)await deferQuery;
    const q=JSON.parse(options.body);answerRequest=q;
    if(q.mode==='order')return {verdict:'SUPPORTED',relation:'SAME_TIMESTAMP',timestamps:['same','same']};
    if(q.mode==='ancestry')return {verdict:'INDETERMINATE',reason:'A program link is missing at earlier-program',chain:[{process_id:'child',parent_id:'earlier-program'}]};
    return {verdict:'SUPPORTED',reason:'Recorded details',returned:100,total_matches:150,offset:q.offset||0,next_offset:100,answer:Array.from({length:100},(_,i)=>({principal:'LAB analyst',event_id:String(i)}))};
  }
  if(url==='/api/segment/2')return segments.find(item=>item.id===2);
  return {...old,id:url==='/api/segment/99'?99:1};
}});
const context=vm.createContext({document,Option,fetch,console});
(async()=>{
  vm.runInContext(script,context);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements.contract.options[0].value,'OLD');
  assert.equal(elements.contract.options[0].textContent,'Historical capability');
  // 1: Historical rules are authoritative and show their saved human name.
  assert.match(elements.policyScope.textContent,/rules saved with this group/);
  // 2: Basic search needs no JSON.
  assert.equal(JSON.stringify(vm.runInContext('readFilters()',context)),'{}');
  // 3: Simple named condition maps to the stable engine field.
  elements.filterField.value='process_id';elements.filterValue.value='proc-2';
  assert.equal(JSON.stringify(vm.runInContext('readFilters()',context)),JSON.stringify({process_id:'proc-2'}));
  // 4: Hidden advanced text cannot silently override the simple search.
  elements.filters.value='{bad';assert.equal(vm.runInContext('readFilters().process_id',context),'proc-2');
  // 5: Multiple explicit conditions still work.
  elements.useAdvanced.checked=true;elements.filters.value='{"action":"connect","principal":"LAB analyst"}';
  assert.equal(vm.runInContext('Object.keys(readFilters()).length',context),2);
  // 6-7: Bad advanced syntax/types explain the problem.
  elements.filters.value='{bad';assert.throws(()=>vm.runInContext('readFilters()',context),/not valid JSON/);
  elements.filters.value='[]';assert.throws(()=>vm.runInContext('readFilters()',context),/field name and a text value/);
  elements.useAdvanced.checked=false;
  // 8: Human-readable answer and a real result table, with continuation.
  await vm.runInContext("executeQuery({segment_id:1,mode:'records',contract:'OLD',offset:0})",context);
  assert.equal(elements.verdict.textContent,'Answer available');assert.equal(elements.next.disabled,false);
  assert.match(elements.resultSummary.textContent,/1 to 100 of 150/);assert.equal(elements.answerTable.children.length,1);
  // 9: Editing invalidates the displayed answer and its continuation.
  elements.filterValue.handlers.input();assert.equal(elements.next.disabled,true);assert.equal(elements.answerTable.children.length,0);
  // 10: Incomplete program history is explicitly incomplete, with partial links.
  await vm.runInContext("executeQuery({segment_id:1,mode:'ancestry'})",context);
  assert.equal(elements.verdict.textContent,'Not enough information');assert.equal(elements.answerTable.children.length,1);
  // 11: Equal times do not acquire an invented order.
  await vm.runInContext("executeQuery({segment_id:1,mode:'order',event_a:'1',event_b:'2'})",context);
  assert.match(elements.resultSummary.textContent,/order cannot be decided/);
  // 12-13: Direct selection survives a page refresh.
  await vm.runInContext('inspect(99)',context);assert.equal(elements.segment.value,'99');
  await vm.runInContext('refresh()',context);assert.equal(elements.segment.value,'99');
  // 14: Changing a question while it is running cannot display a stale answer.
  let release;deferQuery=new Promise(resolve=>{release=resolve});
  const running=vm.runInContext("executeQuery({segment_id:99,mode:'records',contract:'OLD'})",context);
  vm.runInContext('invalidateQuery()',context);release();await running;deferQuery=null;
  assert.match(elements.notice.textContent,/question changed while/);assert.equal(elements.next.disabled,true);
  // 15: A default preset must not silently clear custom rules and allow discard.
  assert.throws(()=>vm.runInContext("preset(['EC-01','EC-02','EC-05'])",context),/Choose the available rules manually/);
  assert.equal(vm.runInContext('selectedRules().length',context),1);
  // 16: Empty storage explains the next action and disables questions.
  segments=[];elements.segment.replaceChildren();await vm.runInContext('refresh()',context);
  assert.equal(elements.query.disabled,true);assert.match(elements.segments.children[0].children[0].textContent,/No saved records yet/);
  // 17: The guided button adds the bounded reduced example and opens a prepared question.
  currentPolicy=JSON.parse(fs.readFileSync(path.join(root,'contracts.json'),'utf8'));
  await elements.start.onclick();
  assert.equal(guidedBody.count,24);assert.deepEqual(guidedBody.allowed_levels,['F1','F2']);
  assert.equal(elements.segment.value,'2');assert.equal(elements.filterField.value,'process_id');
  assert.equal(elements.verdict.textContent,'Answer available');
  assert.match(elements.notice.textContent,/Your example is ready/);
  // 18: Live results use the full candidate and actual retained bytes from the same batch.
  vm.runInContext("show('benchmark')",context);
  assert.equal(elements.liveResults.hidden,false);
  const liveBars=elements.liveChart.children[0].children;
  assert.equal(liveBars[0].children[1].children[0].style,'width:100%');
  assert.equal(liveBars[1].children[1].children[0].style,'width:42%');
  assert.match(elements.liveDelta.textContent,/58.0% smaller/);
  assert.equal(elements.details.textContent.includes('policy_version'),false);
  assert.match(elements.homeCompressionContext.textContent,/1 saved batch/);
  assert.match(elements.homeCompressionDelta.textContent,/58.0% smaller/);
  // 19: Empty results distinguish a missing batch from zero bytes.
  segments=[];elements.segment.replaceChildren();await vm.runInContext('refresh()',context);
  assert.equal(elements.resultBatch.disabled,true);
  assert.equal(elements.liveDelta.hidden,true);
  assert.match(elements.liveDetail.textContent,/Add an example or import a file/);
  assert.equal(elements.homeCompressionDelta.hidden,true);
  assert.match(elements.homeCompressionContext.textContent,/Add records/);
  // 20: Benchmark measurements still render in their separate view.
  await elements.loadBench.onclick();
  assert.equal(elements.liveResults.hidden,true);
  assert.equal(elements.benchRows.children.length,benchmark.cases.length);
  assert.match(elements.benchRows.children[0].children[6].textContent,/ms$/);
  assert.match(elements.benchRows.children[0].children[7].textContent,/ms$/);
  const sampleIndex=benchmark.cases.findIndex(item=>item.scenario==='diverse' && item.codec==='zlib');
  elements.benchCase.value=sampleIndex;
  elements.benchCase.onchange();
  const widths=chart=>chart.children[0].children.map(item=>item.children[1].children[0].style);
  assert.equal(widths(elements.benchChart).length,2);
  // 21: Answer search reruns the last question from the first page and can be cleared.
  await elements.start.onclick();
  elements.answerSearch.value='  ANALYST  ';
  await elements.answerSearchForm.onsubmit({preventDefault(){}});
  assert.equal(answerRequest.search,'ANALYST');
  assert.equal(answerRequest.offset,0);
  assert.equal(elements.clearAnswerSearch.hidden,false);
  await elements.clearAnswerSearch.onclick();
  assert.equal(answerRequest.search,'');
  assert.equal(elements.notice.textContent,'');
  // 22: Cancelling deletion preserves the row; confirming clears the selected batch and summary.
  const remove=elements.segments.children[0].children[5].children[0].children[1];
  context.window={confirm:()=>false};
  await remove.onclick();
  assert.equal(segments.length,1);
  context.window.confirm=()=>true;
  await remove.onclick();
  assert.equal(segments.length,0);
  assert.equal(elements.export.hidden,true);
  assert.equal(elements.answerSearchForm.hidden,true);
  assert.equal(elements.homeCompressionDelta.hidden,true);
  assert.match(elements.notice.textContent,/Deleted group/);
  console.log('22 screen-state checks passed (DOM simulation; no browser layout validation).');
})().catch(error=>{console.error(error);process.exitCode=1;});
