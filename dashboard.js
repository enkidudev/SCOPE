'use strict';

// The engine keeps stable field names. This file gives them plain screen labels.
const $ = id => document.getElementById(id);
const fieldNames = {
  source_id: 'Computer/source', event_id: 'Record number', event_type: 'Record type',
  timestamp: 'Recorded time', principal: 'User', session_id: 'Session', action: 'Action',
  process_id: 'Program reference', parent_id: 'Started by program',
  privilege_before: 'Permissions before', privilege_after: 'Permissions after',
  remote_ip: 'Remote address', direction: 'Connection direction', raw_payload: 'Extra message'
};
const usualRules = {
  'EC-01': ['User, session and action', ['principal', 'session_id', 'action']],
  'EC-02': ['Programs and the programs that started them', ['process_id', 'parent_id']],
  'EC-03': ['Before-and-after permission details', ['process_id', 'privilege_before', 'privilege_after', 'timestamp']],
  'EC-04': ['Programs, remote addresses and connection times', ['process_id', 'remote_ip', 'direction', 'timestamp']],
  'EC-05': ['Record numbers and times', ['event_id', 'timestamp']]
};
const storageNames = {
  F0: 'Full checked records', F1: 'Required details only', F2: 'Repeated details stored together',
  F4: 'Checking value only', F5: 'No record content'
};
const packingNames = {none: 'Uncompressed', zlib: 'zlib', zstd: 'Zstandard'};
const exampleNames = {repetitive: 'Repeated activity', diverse: 'Mixed activity', entropy: 'Long extra messages', chain: 'Program family'};
let state, segmentOffset = 0, activeSegment = null, selectedRecord = null;
let lastQuery = null, nextOffset = null, policyRequest = 0, questionVersion = 0;
let resultsRecords = [], benchmarkRequest, benchmarkData, lastIngestId;
let queryRequest = 0, inspectionRequest = 0;
const views = ['home', 'overview', 'ingest', 'explorer', 'investigation', 'benchmark', 'guide'];

function fieldName(name) { return fieldNames[name] || name.replaceAll('_', ' '); }
function ruleName(id, rule) {
  const usual = usualRules[id];
  const sameFields = usual && JSON.stringify([...usual[1]].sort()) === JSON.stringify([...rule.fields].sort());
  return sameFields ? usual[0] : rule.name;
}
function size(bytes) {
  if (bytes < 1000) return bytes.toLocaleString() + ' bytes';
  if (bytes < 1000000) return (bytes / 1000).toFixed(1) + ' KB';
  return (bytes / 1000000).toFixed(2) + ' MB';
}
function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function cell(row, text) { const item = node('td', text); row.append(item); return item; }
function metric(parent, name, value) {
  const item = node('div', undefined, 'metric');
  item.append(node('small', name), node('strong', value)); parent.append(item);
}
function show(view, navigate = true) {
  document.querySelectorAll('.panel').forEach(item => item.hidden = item.id !== view);
  $('workspaceNav').hidden = view === 'home' || view === 'guide';
  document.querySelectorAll('nav button').forEach(item => item.setAttribute('aria-current', item.dataset.view === view ? 'page' : 'false'));
  if (typeof window !== 'undefined' && navigate && window.location.hash !== '#' + view) {
    window.history.pushState(null, '', '#' + view);
  }
  if (view === 'benchmark') showResults(false);
}
function route() {
  const view = window.location.hash.slice(1);
  if (view === 'main') return;
  show(views.includes(view) ? view : 'home', false);
}
function message(text, kind = 'success') { $('notice').textContent = text; $('notice').dataset.kind = kind; }
async function api(url, data) {
  let response;
  try {
    response = await fetch(url, data === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
  } catch (_) { throw Error('Cannot reach SCOPE. Keep the program running in its terminal, then try again.'); }
  const result = await response.json();
  if (!response.ok) {
    let text = result.error || 'This action could not be completed.';
    if (text.startsWith('Cannot save this group with the selected rules.')) {
      try {
        const missing = JSON.parse(text.slice(text.indexOf('{')));
        const items = Object.entries(missing).filter(([, fields]) => fields.length).map(([id, fields]) => {
          const rule = state?.policy?.contracts[id];
          return (rule ? ruleName(id, rule) : id) + ' needs ' + fields.map(fieldName).join(', ');
        });
        text = items.length ? 'Cannot add these records. ' + items.join('; ') + '. Choose rules that match the details in your file.' : 'No allowed storage choice keeps the required details. Check the storage settings.';
      } catch (_) { /* Keep the original explanation if its detail format changes. */ }
    }
    for (const [field, name] of Object.entries(fieldNames)) text = text.replaceAll(field, name.toLowerCase());
    throw Error(text);
  }
  return result;
}
async function run(action) {
  message('Working...', 'busy');
  try { await action(); if ($('notice').dataset.kind === 'busy') message(''); }
  catch (error) { message(error.message, 'error'); }
}
async function busy(button, action) {
  button.disabled = true; button.setAttribute('aria-busy', 'true');
  try { await run(action); }
  finally { button.removeAttribute('aria-busy'); button.disabled = button.id === 'query' && !$('segment').value; }
}
function invalidateQuery(text = 'Question changed. Choose Find answer to run it again.') {
  questionVersion += 1; lastQuery = null; nextOffset = null; $('next').disabled = true;
  $('answerSearchForm').hidden = true; $('answerSearch').value = '';
  $('verdict').textContent = 'Your answer'; $('verdict').className = '';
  $('resultSummary').textContent = text; $('answerTable').replaceChildren();
  $('result').textContent = 'No current answer. Run this question again.';
}
function selectedRules() { return [...$('contracts').querySelectorAll('input:checked')].map(item => item.value); }
function updateRuleSummary() {
  const count = selectedRules().length;
  $('ruleSummary').textContent = count ? count + ' rules selected. Every record must contain the needed details.' :
    'No rules selected. SCOPE may remove all record content; only group information and storage notes may remain.';
}
function preset(ids) {
  const boxes = [...$('contracts').querySelectorAll('input')];
  if (ids && ids.length && !ids.every(id => boxes.some(box => box.value === id))) {
    throw Error('This shortcut needs the standard SCOPE rules. Choose the available rules manually for this rule file.');
  }
  boxes.forEach(box => box.checked = ids === null || ids.includes(box.value)); updateRuleSummary();
}
function buildRules() {
  if ($('contracts').children.length) return;
  for (const [id, rule] of Object.entries(state.policy.contracts)) {
    const label = node('label', undefined, 'check');
    const box = node('input'); box.type = 'checkbox'; box.value = id; box.checked = true;
    box.addEventListener('change', updateRuleSummary);
    label.append(box, document.createTextNode(ruleName(id, rule)), node('small', 'Keeps: ' + rule.fields.map(fieldName).join(', ')));
    $('contracts').append(label);
  }
  updateRuleSummary();
}
async function syncQueryPolicy() {
  const id = $('segment').value, token = ++policyRequest;
  const changed = id !== activeSegment;
  if (changed) { invalidateQuery('Choose a question for this saved group.'); activeSegment = id; }
  if (!id) {
    $('contract').replaceChildren(); $('query').disabled = true;
    $('policyScope').textContent = 'No saved groups yet. Add an example or a file first.'; return;
  }
  const record = state?.segments.find(item => String(item.id) === id && item.integrity !== 'FAILED') || await api('/api/segment/' + id);
  if (token !== policyRequest) return;
  const previous = $('contract').value; $('contract').replaceChildren();
  for (const [cid, rule] of Object.entries(record.contracts)) $('contract').add(new Option(ruleName(cid, rule), cid));
  if ([...$('contract').options].some(option => option.value === previous)) $('contract').value = previous;
  $('policyScope').textContent = 'Group #' + id + ' · ' + record.sources.join(', ') + '. Questions use the rules saved with this group.';
  if (changed) $('source').value = record.sources[0] || '';
  $('query').disabled = false;
}
function keptRules(record) {
  return Object.entries(record.descriptor).filter(([, detail]) => detail.status === 'EXACT')
    .map(([id]) => ruleName(id, record.contracts[id]));
}
function renderStorage(stats) {
  $('storage').replaceChildren();
  const list = node('ul', undefined, 'plain-list');
  for (const [name, value] of [
    ['Original input', stats.input_bytes], ['Saved records and reading details', stats.encoded_document_bytes],
    ['Storage-choice notes', stats.candidate_audit_bytes], ['Search notes', stats.query_summary_bytes],
    ['Full database file allocation', stats.sqlite_allocated_bytes]
  ]) list.append(node('li', name + ': ' + size(value)));
  $('storage').append(list);
}
async function refresh() {
  state = await api('/api/state?limit=50&offset=' + segmentOffset); buildRules();
  for (const id of ['start', 'demo', 'import']) $(id).disabled = false;
  if (segmentOffset === 0) resultsRecords = state.segments;
  renderLiveResults(); renderHomeCompression();
  const previous = $('segment').value; $('segment').replaceChildren(); $('segments').replaceChildren();
  for (const record of state.segments) {
    const row = node('tr'); cell(row, '#' + record.id);
    const actions = node('div', undefined, 'record-actions');
    if (record.integrity === 'FAILED') {
      const item = cell(row, 'Cannot read this group. ' + record.reason); item.colSpan = 4;
    } else {
      cell(row, storageNames[record.level] || record.level); cell(row, record.source_event_count);
      cell(row, size(record.encoded_document_bytes)); cell(row, keptRules(record).join('; ') || 'No complete rule kept');
      const open = node('button', 'Open');
      open.setAttribute('aria-label', 'Open group #' + record.id);
      open.onclick = () => run(() => inspect(record.id));
      actions.append(open);
      $('segment').add(new Option('#' + record.id + ' · ' + record.source_event_count + ' records', record.id));
    }
    const remove = node('button', 'Delete', 'record-delete');
    remove.setAttribute('aria-label', 'Delete group #' + record.id);
    remove.onclick = () => {
      if (window.confirm('Delete group #' + record.id + ' and its investigation history? This cannot be undone.')) {
        return busy(remove, () => deleteGroup(record.id));
      }
    };
    actions.append(remove);
    cell(row, '').append(actions);
    $('segments').append(row);
  }
  if (previous && !state.segments.some(item => String(item.id) === previous)) $('segment').add(new Option('#' + previous + ' · opened by number', previous));
  if ([...$('segment').options].some(option => option.value === previous)) $('segment').value = previous;
  if (!state.segments.length) {
    const row = node('tr'), item = cell(row, segmentOffset ? 'No more groups here. Go back to the newest groups.' : 'No saved records yet. Try the guided example in Overview or add a file.');
    item.colSpan = 6; item.className = 'empty'; $('segments').append(row);
  }
  const stats = state.stats; $('metrics').replaceChildren();
  metric($('metrics'), 'Saved groups', stats.segments); metric($('metrics'), 'Original input', size(stats.input_bytes));
  metric($('metrics'), 'Saved record size', size(stats.encoded_document_bytes)); metric($('metrics'), 'Questions asked', stats.queries);
  renderStorage(stats); $('older').disabled = state.segments.length < 50; $('newer').disabled = segmentOffset === 0;
  await syncQueryPolicy();
}
function describeGroup(record, target) {
  target.replaceChildren();
  target.append(node('p', 'Group #' + record.id + ' contains ' + record.source_event_count + ' records from ' + record.sources.join(', ') + '.'));
  target.append(node('p', 'Saved as: ' + (storageNames[record.level] || record.level) + '. ' + (packingNames[record.codec] || record.codec) + '. Size: ' + size(record.encoded_document_bytes) + '.'));
  const kept = keptRules(record);
  target.append(node('p', kept.length ? 'Details kept for all records:' : 'No listed rule was fully kept. Questions may have too little information.'));
  const list = node('ul', undefined, 'plain-list'); kept.forEach(name => list.append(node('li', name))); target.append(list);
  if (record.field_coverage.raw_payload === 'UNAVAILABLE') target.append(node('p', 'Extra messages were not kept for every record. A question requiring them will have too little information.', 'note'));
}
function displayRecord(record) {
  const {schema_version, policy_version, payload, ...details} = record;
  details.descriptor = Object.fromEntries(Object.entries(record.descriptor).map(([id, detail]) => {
    const {method, ...coverage} = detail;
    return [id, coverage];
  }));
  return details;
}
async function inspect(id) {
  const token = ++inspectionRequest;
  const record = await api('/api/segment/' + id);
  if (token !== inspectionRequest) return;
  selectedRecord = record;
  describeGroup(record, $('groupSummary'));
  $('details').textContent = JSON.stringify(displayRecord(record), null, 2);
  $('export').href = '/api/export/' + id; $('export').hidden = false; $('askSelected').disabled = false;
  if (![...$('segment').options].some(option => option.value === String(id))) $('segment').add(new Option('#' + id + ' · opened by number', id));
  $('segment').value = String(id); await syncQueryPolicy();
}
async function deleteGroup(id) {
  await api('/api/delete', {segment_id: id});
  inspectionRequest += 1;
  if (selectedRecord?.id === id) {
    selectedRecord = null;
    $('groupSummary').replaceChildren(node('p', 'Choose a group above.', 'muted'));
    $('details').textContent = 'No group selected.';
    $('export').hidden = true;
    $('export').removeAttribute('href');
    $('askSelected').disabled = true;
  }
  if (lastIngestId === id) {
    lastIngestId = null;
    $('selectionSummary').replaceChildren(node('p', 'Add records to see the chosen storage method and details kept.', 'muted'));
    $('selection').textContent = 'No group added yet.';
  }
  if ($('segment').value === String(id)) {
    $('segment').replaceChildren();
    invalidateQuery('Choose a group and ask a question.');
  }
  segmentOffset = 0;
  await refresh();
  message('Deleted group #' + id + ' and its investigation history.');
}

async function added(record) {
  lastIngestId = record.id;
  describeGroup(record, $('selectionSummary')); $('selection').textContent = JSON.stringify(displayRecord(record), null, 2);
  segmentOffset = 0; $('resultBatch').value = ''; await refresh(); await inspect(record.id);
  message('Added group #' + record.id + '. Open Saved records to inspect it or Ask questions to use it.');
}
function updateMode() {
  invalidateQuery(); const mode = $('mode').value;
  $('recordFields').hidden = mode !== 'records'; $('relationFields').hidden = mode === 'records';
  $('ancestryFields').hidden = mode !== 'ancestry'; $('orderFields').hidden = mode !== 'order';
}
function readFilters() {
  if ($('useAdvanced').checked) {
    let filters;
    try { filters = JSON.parse($('filters').value); }
    catch (_) { throw Error('The conditions are not valid JSON. Use the simple search, or copy an example from the guide.'); }
    if (!filters || Array.isArray(filters) || typeof filters !== 'object' || Object.values(filters).some(value => typeof value !== 'string'))
      throw Error('Each condition needs a field name and a text value. Example: {"action":"connect"}.');
    return filters;
  }
  const field = $('filterField').value;
  return field ? {[field]: $('filterValue').value} : {};
}
function question() {
  if (!$('segment').value) throw Error('Add or choose a saved group first.');
  const request = {segment_id: Number($('segment').value), mode: $('mode').value};
  if (request.mode === 'records') Object.assign(request, {contract: $('contract').value, filters: readFilters(), limit: 100, offset: 0});
  else Object.assign(request, {source_id: $('source').value, process_id: $('process').value, event_a: $('eventA').value, event_b: $('eventB').value});
  return request;
}
function renderTable(records) {
  $('answerTable').replaceChildren(); if (!records.length) return;
  const fields = [...new Set(records.flatMap(record => Object.keys(record)))];
  const table = node('table'), head = node('thead'), row = node('tr');
  fields.forEach(field => row.append(node('th', fieldName(field)))); head.append(row); table.append(head);
  const body = node('tbody');
  for (const record of records) { const row = node('tr'); fields.forEach(field => cell(row, record[field] ?? '')); body.append(row); }
  table.append(body); const wrapper = node('div', undefined, 'tablewrap'); wrapper.append(table); $('answerTable').append(wrapper);
}
function explainResult(result) {
  if (result.verdict === 'UNSUPPORTED') return 'This question is not available for this group. Choose another question or review the saved rules.';
  if (result.verdict === 'INDETERMINATE') {
    if (result.missing_fields?.length) return 'Cannot check: ' + result.missing_fields.map(fieldName).join(', ') + '. These details were not kept for every record.';
    return result.reason || 'The saved information is not enough to answer this question.';
  }
  return result.reason || 'The needed recorded details are available.';
}
async function executeQuery(request) {
  const token = questionVersion, requestId = ++queryRequest;
  const result = await api('/api/query', request);
  if (requestId !== queryRequest) return;
  if (token !== questionVersion) { message('Your question changed while the search was running. Choose Find answer again.'); return; }
  $('result').textContent = JSON.stringify(result, null, 2); $('answerTable').replaceChildren();
  const supported = result.verdict === 'SUPPORTED';
  const searchable = supported && request.mode === 'records';
  $('answerSearchForm').hidden = !searchable;
  $('answerSearch').value = request.search || '';
  $('clearAnswerSearch').hidden = !request.search;
  $('verdict').textContent = supported ? (searchable && !request.search && result.total_matches === 0 ? 'No matches in this group' : 'Answer available') : result.verdict === 'INDETERMINATE' ? 'Not enough information' : 'This question is not available';
  $('verdict').className = supported ? 'status' : 'warning';
  if (searchable) {
    const from = result.returned ? result.offset + 1 : 0, to = result.offset + result.returned;
    $('resultSummary').textContent = result.total_matches
      ? 'Showing records ' + from + ' to ' + to + ' of ' + result.total_matches + (request.search ? ' search matches.' : ' matches in this saved group.')
      : request.search ? 'No answer records match this search.' : 'No saved records match these conditions.';
    renderTable(result.answer);
  } else if (supported && request.mode === 'order') {
    const names = {BEFORE: 'The first record has an earlier time.', AFTER: 'The first record has a later time.', SAME_TIMESTAMP: 'Both records have the same time. Their order cannot be decided from these times.'};
    $('resultSummary').textContent = names[result.relation];
    renderTable([{event_id: request.event_a, timestamp: result.timestamps[0]}, {event_id: request.event_b, timestamp: result.timestamps[1]}]);
  } else {
    $('resultSummary').textContent = explainResult(result); if (result.chain) renderTable(result.chain);
  }
  lastQuery = request; nextOffset = result.next_offset ?? null; $('next').disabled = nextOffset === null;
  await refresh();
}

// Button names match the complete guide. Old API keys remain stable for files and commands.
document.querySelectorAll('nav button').forEach(button => button.onclick = () => show(button.dataset.view));
$('all').onclick = () => preset(null); $('none').onclick = () => preset([]);
$('processPreset').onclick = () => run(() => preset(['EC-01', 'EC-02', 'EC-05']));
$('networkPreset').onclick = () => run(() => preset(['EC-04', 'EC-05']));
$('segment').onchange = () => run(syncQueryPolicy); $('mode').onchange = updateMode;
$('filterField').onchange = () => { $('filterValue').disabled = !$('filterField').value; invalidateQuery(); };
for (const id of ['contract', 'filterValue', 'filters', 'source', 'process', 'eventA', 'eventB']) $(id).addEventListener('input', () => invalidateQuery());
$('useAdvanced').onchange = () => { $('filterField').disabled = $('useAdvanced').checked; $('filterValue').disabled = $('useAdvanced').checked || !$('filterField').value; invalidateQuery(); };
$('openSegment').onclick = () => run(async () => {
  const id = Number($('segmentId').value); if (!Number.isInteger(id) || id < 1) throw Error('Enter a group number such as 1 or 2.'); await inspect(id);
});
$('older').onclick = () => run(async () => { segmentOffset += 50; await refresh(); });
$('newer').onclick = () => run(async () => { segmentOffset = 0; await refresh(); });
$('askSelected').onclick = () => run(async () => { if (selectedRecord) { await inspect(selectedRecord.id); show('investigation'); } });
$('start').onclick = () => busy($('start'), async () => {
  const record = await api('/api/demo', {count: 24, scenario: 'repetitive', mandatory: Object.keys(state.policy.contracts), allowed_levels: ['F1', 'F2'], codec: 'none'});
  await added(record); $('mode').value = 'records'; updateMode(); $('filterField').value = 'process_id'; $('filterValue').value = 'proc-guid-2'; $('filterValue').disabled = false; $('useAdvanced').checked = false; $('filterField').disabled = false;
  if ([...$('contract').options].some(option => option.value === 'EC-01')) $('contract').value = 'EC-01';
  show('investigation'); await executeQuery(question()); message('Your example is ready. Read the answer, then choose Ask about a removed message.');
});
$('demo').onclick = () => busy($('demo'), async () => added(await api('/api/demo', {scenario: $('scenario').value, mandatory: selectedRules(), codec: $('codec').value})));
$('import').onclick = () => busy($('import'), async () => {
  const file = $('file').files[0]; if (!file) throw Error('Choose a JSON or XML file first.');
  if (file.size > 16000000) throw Error('Choose a file smaller than 16 MB. Split larger files into smaller groups.');
  const text = await file.text(), xml = file.name.toLowerCase().endsWith('.xml'); let events;
  if (!xml) { try { events = JSON.parse(text); } catch (_) { throw Error('This file is not valid JSON. Use a sample from the examples folder or an exported XML file.'); } }
  await added(await api(xml ? '/api/import-sysmon' : '/api/ingest', {mandatory: selectedRules(), codec: $('codec').value, ...(xml ? {xml: text} : {events})}));
});
$('query').onclick = () => busy($('query'), () => executeQuery(question()));
$('missingExample').onclick = () => busy($('missingExample'), async () => {
  $('useAdvanced').checked = false; $('filterField').disabled = false; $('filterField').value = 'raw_payload'; $('filterValue').disabled = false; $('filterValue').value = 'anything'; invalidateQuery(); await executeQuery(question());
});
$('next').onclick = () => run(async () => { if (lastQuery && nextOffset !== null) await executeQuery({...lastQuery, offset: nextOffset}); });
function searchAnswer(search) {
  if (lastQuery?.mode !== 'records' || $('answerSearchForm').hidden) return;
  return run(() => executeQuery({...lastQuery, search, offset: 0}));
}
$('answerSearchForm').onsubmit = event => {
  event.preventDefault();
  return searchAnswer($('answerSearch').value.trim());
};
$('clearAnswerSearch').onclick = () => searchAnswer('');

function comparison(target, rows) {
  target.replaceChildren();
  const measured = rows.filter(row => Number.isFinite(row.bytes) && row.bytes >= 0);
  if (measured.length === 0) return;
  const max = Math.max(1, ...measured.map(row => row.bytes));
  const chart = node('div', undefined, 'chart-pair');
  for (const row of measured) {
    const line = node('div', undefined, 'chart-line');
    const value = node('strong', size(row.bytes));
    value.setAttribute('title', row.bytes.toLocaleString() + ' bytes');
    line.append(node('span', row.name), value);
    const track = node('div', undefined, 'chart-track');
    track.setAttribute('aria-hidden', 'true');
    const bar = node('span', undefined, row.selected ? 'chart-bar retained' : 'chart-bar');
    bar.setAttribute('style', 'width:' + (row.bytes / max * 100) + '%');
    track.append(bar);
    const item = node('div');
    item.append(line, track);
    chart.append(item);
  }
  target.append(chart);
}

function sizeChange(full, retained) {
  if (!Number.isFinite(full) || full <= 0 || !Number.isFinite(retained)) return '—';
  const difference = (full - retained) / full * 100;
  if (difference === 0) return 'Same size';
  const amount = Math.abs(difference);
  return (amount < 0.1 ? '<0.1' : amount.toFixed(1)) + '% ' + (difference > 0 ? 'smaller' : 'larger');
}

function renderLiveResults() {
  const stats = state.stats;
  $('liveMetrics').replaceChildren();
  metric($('liveMetrics'), 'Saved batches', stats.segments);
  metric($('liveMetrics'), 'Canonical input', size(stats.input_bytes));
  metric($('liveMetrics'), 'Retained documents', size(stats.encoded_document_bytes));
  metric($('liveMetrics'), 'SQLite allocation', size(stats.sqlite_allocated_bytes));
  const picker = $('resultBatch');
  const previous = picker.value;
  picker.replaceChildren();
  for (const record of resultsRecords) {
    picker.add(new Option('#' + record.id + (record.integrity === 'FAILED' ? ' · unreadable' : ' · ' + record.source_event_count.toLocaleString() + ' events'), record.id));
  }
  if ([...picker.options].some(option => option.value === previous)) picker.value = previous;
  picker.disabled = resultsRecords.length === 0;
  renderLiveBatch();
}

function renderLiveBatch() {
  const record = resultsRecords.find(item => String(item.id) === $('resultBatch').value);
  $('liveChart').replaceChildren();
  $('candidateTable').replaceChildren();
  $('liveDelta').hidden = true;
  if (!record) {
    $('liveDetail').textContent = 'Add an example or import a file to measure a batch.';
    return;
  }
  if (record.integrity === 'FAILED') {
    $('liveDetail').textContent = 'Batch #' + record.id + ' is unreadable. ' + record.reason;
    return;
  }
  const candidates = record.candidates || [];
  const full = candidates.find(item => item.level === 'F0')?.encoded_bytes;
  $('liveDetail').textContent = 'Batch #' + record.id + ' · ' + (packingNames[record.codec] || record.codec) +
    (Number.isFinite(full) ? ' for both documents. Sizes exclude SQLite overhead.' : ' · full-size baseline unavailable.');
  comparison($('liveChart'), [
    {name: 'Full records', bytes: full},
    {name: 'Retained document', bytes: record.encoded_document_bytes, selected: true}
  ]);
  if (Number.isFinite(full)) {
    $('liveDelta').textContent = sizeChange(full, record.encoded_document_bytes) + ' compared with full records';
    $('liveDelta').hidden = false;
  }
  if (!candidates.length) {
    $('candidateTable').append(node('p', 'Candidate measurements are unavailable for this batch.', 'muted'));
    return;
  }
  const table = node('table');
  const headings = node('tr');
  for (const label of ['Storage method', 'Encoded size', 'Decision', 'Unmet rules']) {
    const heading = node('th', label);
    heading.setAttribute('scope', 'col');
    headings.append(heading);
  }
  const head = node('thead'), body = node('tbody');
  head.append(headings);
  for (const item of candidates) {
    const row = node('tr');
    cell(row, storageNames[item.level] || item.level);
    cell(row, size(item.encoded_bytes)).setAttribute('title', item.encoded_bytes.toLocaleString() + ' bytes');
    cell(row, item.level === record.level ? 'Selected' : item.admissible ? 'Eligible' : item.allowed ? 'Unmet rules' : 'Excluded by settings');
    cell(row, item.failed_contracts.join(', ') || '—');
    body.append(row);
  }
  table.append(head, body);
  $('candidateTable').append(table);
}

function loadBenchmark() {
  if (!benchmarkRequest) {
    benchmarkRequest = api('/api/benchmark').catch(error => {
      benchmarkRequest = undefined;
      throw error;
    });
  }
  return benchmarkRequest;
}

function showResults(bench) {
  $('liveResults').hidden = bench;
  $('benchmarkResults').hidden = !bench;
  $('liveMode').setAttribute('aria-pressed', String(!bench));
  $('loadBench').setAttribute('aria-pressed', String(bench));
}

function benchmarkLabel(item) {
  return (exampleNames[item.scenario] || item.scenario) + ' · ' + (packingNames[item.codec] || item.codec);
}

function chartBenchmark(target, item) {
  comparison(target, [
    {name: 'Full records', bytes: item.f0_document_bytes},
    {name: 'Retained document', bytes: item.scope_document_bytes, selected: true}
  ]);
}

function renderHomeCompression() {
  const totals = state.compression;
  const chart = $('homeCompressionChart'), delta = $('homeCompressionDelta');
  chart.replaceChildren();
  delta.hidden = true;
  if (!totals?.batches) {
    $('homeCompressionContext').textContent = state.stats.segments
      ? 'No saved batches have a full-size baseline.'
      : 'Add records in the analyst workspace to see measured compression here.';
    return;
  }
  const count = totals.batches.toLocaleString() + (totals.batches === 1 ? ' saved batch' : ' saved batches');
  const excluded = state.stats.segments - totals.batches;
  $('homeCompressionContext').textContent = count + ' · same codec within each batch' +
    (excluded ? ' · ' + excluded + ' without a full-size baseline' : '');
  comparison(chart, [
    {name: 'Full records', bytes: totals.full_bytes},
    {name: 'Retained documents', bytes: totals.retained_bytes, selected: true}
  ]);
  delta.textContent = sizeChange(totals.full_bytes, totals.retained_bytes) + ' across measured batches';
  delta.hidden = false;
}

function renderBenchmarkCase() {
  const item = benchmarkData?.cases[Number($('benchCase').value)];
  if (item) chartBenchmark($('benchChart'), item);
}

function timeMs(value) {
  return Number.isFinite(value) ? value.toFixed(2) + ' ms' : '—';
}

async function renderBenchmark() {
  if (benchmarkData) return;
  $('benchmarkNote').textContent = 'Loading recorded benchmark measurements…';
  let result;
  try {
    result = await loadBenchmark();
  } catch (error) {
    $('benchmarkNote').textContent = error.message + '. Select Benchmarks to retry.';
    return;
  }
  benchmarkData = result;
  $('benchmarkNote').textContent = 'Recorded synthetic benchmarks · ' + result.trials_per_case + ' timing trials per case';
  $('quality').replaceChildren();
  metric($('quality'), 'Protected checks passed', result.quality.correct_protected_checks + '/' + result.quality.protected_checks);
  metric($('quality'), 'Incorrect definite answers', result.quality.incorrect_determinate_checks);
  metric($('quality'), 'Indeterminate checks passed', result.quality.correct_indeterminate_checks + '/' + result.quality.expected_indeterminate_checks);
  metric($('quality'), 'Size comparisons', result.cases.length);
  $('benchCase').replaceChildren();
  $('benchRows').replaceChildren();
  for (const [index, item] of result.cases.entries()) {
    $('benchCase').add(new Option(benchmarkLabel(item), index));
    const row = node('tr');
    for (const value of [exampleNames[item.scenario] || item.scenario, packingNames[item.codec], storageNames[item.selected_level], size(item.f0_document_bytes), size(item.scope_document_bytes), sizeChange(item.f0_document_bytes, item.scope_document_bytes), timeMs(item.ingest_wall_ms_median), timeMs(item.query_ms_median)]) cell(row, value);
    $('benchRows').append(row);
  }
  $('benchCase').disabled = !result.cases.length;
  const limitations = node('ul', undefined, 'plain-list');
  for (const note of result.limitations) limitations.append(node('li', note));
  $('benchData').replaceChildren(node('p', result.method), limitations);
  renderBenchmarkCase();
}

$('resultBatch').onchange = renderLiveBatch;
$('liveMode').onclick = () => showResults(false);
$('loadBench').onclick = () => { showResults(true); return renderBenchmark(); };
$('benchCase').onchange = renderBenchmarkCase;
if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', route);
  route();
}
run(refresh);
