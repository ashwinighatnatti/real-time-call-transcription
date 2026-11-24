# Front end. The form is built from the field lists the server sends (see fields.py),
# and all transcript/field text is inserted with textContent (no HTML injection).
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Call Monitoring</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Tahoma,sans-serif;background:#3C3C3C;padding:20px;min-height:90vh}
  .app{max-width:1600px;margin:0 auto;background:rgba(255,255,255,.95);border-radius:23px;padding:18px;
       display:grid;grid-template-columns:1fr 420px;gap:20px;box-shadow:0 20px 40px rgba(0,0,0,.1)}
  header{grid-column:1/span 2;display:flex;align-items:center;gap:12px}
  header h1{font-weight:300;font-size:20px;color:#222}
  .controls{display:flex;gap:10px;margin-left:auto;align-items:center}
  button,.btn{padding:9px 16px;border-radius:10px;border:none;font-weight:700;cursor:pointer;font-size:13px;
              text-decoration:none;color:#fff;display:inline-block}
  button:disabled{opacity:.5;cursor:default}
  .start{background:linear-gradient(45deg,#4CAF50,#45a049)}
  .stop{background:linear-gradient(45deg,#f44336,#da190b)}
  .clear{background:linear-gradient(45deg,#2196F3,#0b7dda)}
  .export{background:#6c757d}
  .status{grid-column:1/span 2;padding:10px;border-radius:10px;font-weight:700}
  .status.recording{background:#d4edda;color:#155724;border:2px solid #28a745}
  .status.stopped{background:#f8d7da;color:#721c24;border:2px solid #dc3545}
  .warning{background:#fff3cd;color:#664d03;border:2px solid #e0a800;border-radius:10px;padding:10px 14px;margin-bottom:6px;font-weight:600}
  .conversation{border-radius:12px;padding:18px;height:680px;overflow-y:auto;border:2px solid #e0e0e0}
  .info{background:#d1ecf1;padding:14px;border-radius:8px;margin-bottom:10px}
  .message{margin-bottom:14px;display:flex}
  .message.left{justify-content:flex-start}.message.right{justify-content:flex-end}
  .bubble{max-width:70%;padding:10px 14px;border-radius:14px;box-shadow:0 6px 18px rgba(0,0,0,.08)}
  .bubble.left{background:#e3f2fd;border:2px solid #2196F3}
  .bubble.right{background:#f3e5f5;border:2px solid #9c27b0}
  .speaker-label{font-size:12px;font-weight:700;color:#333;margin-bottom:4px;text-transform:uppercase}
  .timestamp{font-size:11px;color:#666;margin-top:6px;text-align:right}
  .form{border-radius:12px;padding:16px;border:2px solid #e2e8f0;height:680px;overflow-y:auto;background:#fff}
  .form h3{color:#003366;margin:4px 0 8px}
  label{font-size:12px;color:#333;display:block;margin-bottom:3px}
  input[type=text]{padding:8px;border-radius:8px;border:1px solid #cbd5e1;width:100%;font-size:13px}
  input.warn{border-color:#e08a00;background:#fff8e6}
  .row{margin-bottom:8px}
  .card{border:1px solid #cbd5e1;border-radius:10px;padding:10px;margin:10px 0;background:#f8fafc}
  .card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-weight:700;color:#003366}
  .inst{border-top:1px dashed #cbd5e1;margin-top:8px;padding-top:8px}
  .small{padding:4px 10px;font-size:12px;background:#007bff}
  .danger{background:#c0392b}
  .save{width:100%;margin-top:12px;background:#007bff;padding:12px}
  @media(max-width:1000px){.app{grid-template-columns:1fr}.form{height:auto}}
</style>
</head>
<body>
<div class="app">
  <header>
    <img src="/static/logo.gif" alt="logo" style="height:36px">
    <h1>AI Call Monitoring</h1>
    <div class="controls">
      <button id="start-btn" class="start" onclick="socket.emit('start_recording')">Start</button>
      <button id="stop-btn" class="stop" onclick="socket.emit('stop_recording')" disabled>Stop</button>
      <button class="clear" onclick="clearAll()">Clear</button>
      <a class="btn export" href="/export.xlsx" target="_blank">Export Excel</a>
      <a class="btn export" href="/export_agencies.csv" target="_blank">Agency CSV</a>
    </div>
  </header>
  <div id="status" class="status stopped">Ready to start</div>
  <div id="warnings" style="grid-column:1/span 2"></div>
  <div class="conversation" id="conversation"></div>
  <div class="form">
    <h3>Property</h3>
    <div id="property"></div>
    <h3>Parcels</h3>
    <div id="parcels"></div>
    <button class="small" onclick="socket.emit('add_parcel')">+ Add parcel</button>
    <button class="save" onclick="socket.emit('save_call')">Save call</button>
  </div>
</div>
<script>
const socket = io();
let CONFIG = null, STATE = null;
const side = {};

function el(tag, cls, text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e;}

function resetConversation(){
  const c = document.getElementById('conversation'); c.replaceChildren();
  const info = el('div','info'); info.appendChild(el('b',null,'Click Start to begin. '));
  info.appendChild(document.createTextNode('The conversation appears here; extracted values fill in on the right. Edit any value to lock it.'));
  c.appendChild(info);
}
resetConversation();

socket.on('config', cfg => { CONFIG = cfg; if (STATE) render(STATE); });
socket.on('state_update', s => { STATE = s; if (CONFIG) render(s); });

socket.on('recording_started', () => {
  document.getElementById('start-btn').disabled = true;
  document.getElementById('stop-btn').disabled = false;
  const s = document.getElementById('status'); s.className='status recording'; s.textContent='Recording...';
  document.getElementById('warnings').replaceChildren();
  resetConversation();
});
socket.on('recording_stopped', () => {
  document.getElementById('start-btn').disabled = false;
  document.getElementById('stop-btn').disabled = true;
  const s = document.getElementById('status'); s.className='status stopped'; s.textContent='Recording stopped. Saved.';
});
socket.on('warning_message', m => {
  document.getElementById('warnings').appendChild(el('div', 'warning', m));
});
socket.on('error_message', m => {
  const s = document.getElementById('status'); s.className='status stopped'; s.textContent = m;
});
socket.on('saved', () => {
  const p = el('div'); p.style.cssText='position:fixed;top:20px;right:20px;background:#fff;padding:14px 18px;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.25);font-weight:700';
  p.textContent = 'Saved to local database'; document.body.appendChild(p); setTimeout(()=>p.remove(), 2500);
});

// ---- conversation
function clearPartial(speaker){
  document.querySelectorAll('.partial-result').forEach(n => { if (n.dataset.speaker === speaker) n.remove(); });
}
function sideOf(speaker){ if(!side[speaker]) side[speaker] = Object.keys(side).length === 0 ? 'left' : 'right'; return side[speaker]; }
function bubble(speaker, text, stamp, partial){
  const c = document.getElementById('conversation');
  const info = c.querySelector('.info'); if (info) info.remove();
  clearPartial(speaker);
  const sd = sideOf(speaker);
  const row = el('div', 'message ' + sd + (partial ? ' partial-result' : '')); row.dataset.speaker = speaker;
  const b = el('div', 'bubble ' + sd);
  b.appendChild(el('div','speaker-label', speaker + (partial ? ' (listening...)' : '')));
  b.appendChild(el('div',null,text));
  if (stamp) b.appendChild(el('div','timestamp',stamp));
  row.appendChild(b); c.appendChild(row); c.scrollTop = c.scrollHeight;
}
socket.on('new_message', m => bubble(m.speaker, m.text, m.timestamp, false));
socket.on('partial_result', m => { if (m.text && m.text.length > 3) bubble(m.speaker, m.text, '', true); });

// ---- form
function field(f, scope, id, value, warning){
  const wrap = el('div','row');
  const lab = el('label',null,f.label);
  const inp = document.createElement('input'); inp.type='text';
  inp.dataset.key = scope + '.' + id + '.' + f.key;
  inp.value = value || '';
  inp.addEventListener('input', () => socket.emit('field_edit', {scope, id, key: f.key, value: inp.value}));
  inp.addEventListener('blur', () => socket.emit('field_edit', {scope, id, key: f.key, value: inp.value, final: true}));
  wrap.append(lab, inp); return wrap;
}
function setInput(root, scope, id, f, value, warnings){
  let inp = root.querySelector('input[data-key="' + scope + '.' + id + '.' + f.key + '"]');
  if (!inp) return;
  if (document.activeElement !== inp) inp.value = value || '';
  const w = warnings[(scope === 'property' ? 'property' : id) + '.' + f.key];
  inp.classList.toggle('warn', !!w); inp.title = w || '';
}

function render(s){
  const w = s.warnings || {};
  const prop = document.getElementById('property');
  if (!prop.children.length) CONFIG.property_fields.forEach(f => prop.appendChild(field(f,'property','property','')));
  CONFIG.property_fields.forEach(f => setInput(prop,'property','property',f,s.property[f.key],w));

  const box = document.getElementById('parcels');
  const ids = s.parcels.map(p => p.id);
  [...box.children].forEach(c => { if (!ids.includes(c.dataset.id)) c.remove(); });
  s.parcels.forEach((p, idx) => {
    let card = box.querySelector('.card[data-id="' + p.id + '"]');
    if (!card) {
      card = el('div','card'); card.dataset.id = p.id;
      const head = el('div','card-head'); head.appendChild(el('span','card-title','Parcel'));
      const del = el('button','small danger','Delete'); del.onclick = () => socket.emit('delete_parcel', {id:p.id});
      head.appendChild(del); card.appendChild(head);
      CONFIG.parcel_fields.forEach(f => card.appendChild(field(f,'parcel',p.id,'')));
      card.appendChild(el('div','agencies'));
      const add = el('button','small','+ Add agency'); add.onclick = () => socket.emit('add_agency', {parcel_id:p.id});
      card.appendChild(add); box.appendChild(card);
    }
    card.querySelector('.card-title').textContent = 'Parcel ' + (idx + 1);
    CONFIG.parcel_fields.forEach(f => setInput(card,'parcel',p.id,f,p[f.key],w));

    const alist = card.querySelector('.agencies');
    const aids = p.agencies.map(a => a.id);
    [...alist.children].forEach(c => { if (!aids.includes(c.dataset.id)) c.remove(); });
    p.agencies.forEach((a, an) => {
      let ac = alist.querySelector('.agency[data-id="' + a.id + '"]');
      if (!ac) {
        ac = el('div','agency inst'); ac.dataset.id = a.id;
        const ahead = el('div','card-head'); ahead.appendChild(el('span','agency-title','Agency'));
        const adel = el('button','small danger','x'); adel.onclick = () => socket.emit('delete_agency', {id:a.id});
        ahead.appendChild(adel); ac.appendChild(ahead);
        CONFIG.agency_fields.forEach(f => ac.appendChild(field(f,'agency',a.id,'')));
        ac.appendChild(el('div','insts'));
        const addi = el('button','small','+ Add installment'); addi.onclick = () => socket.emit('add_installment', {agency_id:a.id});
        ac.appendChild(addi); alist.appendChild(ac);
      }
      ac.querySelector('.agency-title').textContent = 'Agency ' + (an + 1);
      CONFIG.agency_fields.forEach(f => setInput(ac,'agency',a.id,f,a[f.key],w));

      const list = ac.querySelector('.insts');
      const iids = a.installments.map(i => i.id);
      [...list.children].forEach(c => { if (!iids.includes(c.dataset.id)) c.remove(); });
      a.installments.forEach((i, n) => {
        let row = list.querySelector('.inst[data-id="' + i.id + '"]');
        if (!row) {
          row = el('div','inst'); row.dataset.id = i.id;
          const head = el('div','card-head'); head.appendChild(el('span','inst-title','Installment'));
          const del = el('button','small danger','x'); del.onclick = () => socket.emit('delete_installment', {id:i.id});
          head.appendChild(del); row.appendChild(head);
          CONFIG.installment_fields.forEach(f => row.appendChild(field(f,'installment',i.id,'')));
          list.appendChild(row);
        }
        row.querySelector('.inst-title').textContent = 'Installment ' + (n + 1);
        CONFIG.installment_fields.forEach(f => setInput(row,'installment',i.id,f,i[f.key],w));
      });
    });
  });
}

function clearAll(){
  Object.keys(side).forEach(k => delete side[k]);
  resetConversation();
  document.getElementById('parcels').replaceChildren();
  socket.emit('clear_conversation');
}
</script>
</body>
</html>
"""
