// Enhance exported real application screens. This file has no backend actions.
const response = await fetch(new URL('snapshots.json', import.meta.url), {cache: 'no-cache'});
if (!response.ok) throw new Error('Could not load the sample dashboard');
const { bank, pages, shell } = await response.json();
const $ = id => document.getElementById(id);
const metrics = ['Velocity', 'Acceleration', 'Crest Factor', 'Displacement', 'True Peak', 'Std Deviation'];
let channel = '1', modalFocus;
let navigationVersion = 0, historyVersion = 0;
let historyMoment = null, historyView = 'spectrum', historyQuantity = 'acceleration', historySpeed = null;
const historyTools = new Set(['harmonics', 'bearing', 'peaks']);
const historyMetrics = new Set(['Acceleration', 'Velocity']);
const chips = {Acceleration:['Acc RMS','#1f77b4'], Velocity:['Vel RMS','#ff7f0e'], 'True Peak':['0-P','#d62728'],
  Displacement:['Disp','#9467bd'], 'Crest Factor':['CF','#8c564b'], 'Std Deviation':['Std dev','#e377c2']};
const trends = new Set(['1']);
const selectedMetrics = {};
const main = $('page-content');
document.body.classList.toggle('demo-nav-hidden', matchMedia('(max-width: 767px)').matches);
$('demo-shell').innerHTML = shell.header + shell.sidebar;
$('sidebar-toggle-btn').setAttribute('aria-label', 'Toggle navigation menu');
$('sidebar-toggle-btn').setAttribute('aria-controls', 'sidebar');
$('sidebar-toggle-btn').setAttribute('aria-expanded', String(!document.body.classList.contains('demo-nav-hidden')));
$('sidebar-toggle-btn').addEventListener('click', () => {
  document.body.classList.toggle('demo-nav-hidden');
  $('sidebar-toggle-btn').setAttribute('aria-expanded', String(!document.body.classList.contains('demo-nav-hidden')));
  setTimeout(resize, 350);
});
document.querySelectorAll('#sidebar a').forEach(link => {
  const path = link.getAttribute('href');
  link.href = '#' + path;
  link.addEventListener('click', () => {
    if (matchMedia('(max-width: 767px)').matches) document.body.classList.add('demo-nav-hidden');
  });
});
document.querySelector('nav.navbar a').href = '#/';

function pattern(element) {
  try { return JSON.parse(element.id); } catch { return {}; }
}
function clone(value) { return JSON.parse(JSON.stringify(value)); }
async function plot(element, figure) {
  if (!element || !figure) return;
  const spec = clone(figure);
  // SVG traces keep the offline viewer compatible with its strict script policy.
  for (const trace of spec.data) if (trace.type === "scattergl") trace.type = "scatter";
  delete spec.layout.width;
  spec.layout.autosize = true;
  await window.Plotly.react(element, spec.data, spec.layout, {responsive: true, displaylogo: false, displayModeBar: false});
}
function resize() {
  document.querySelectorAll('.js-plotly-plot').forEach(element => window.Plotly.Plots.resize(element));
}
function options(id, choices, value) {
  const select = $(id);
  if (!select) return;
  select.replaceChildren(...choices.map(choice => {
    const option = document.createElement('option');
    option.value = typeof choice === 'string' ? choice : choice.value;
    option.textContent = typeof choice === 'string' ? choice : choice.label;
    return option;
  }));
  select.value = value;
  select.disabled = false;
}
function channelOptions() {
  return Object.entries(bank.channels).map(([id, data]) => ({value: id,
    label: `${data.asset.site} / ${data.asset.machine} / ${data.asset.location} / ${data.asset.direction} — ${bank.gateway} CH${id}`}));
}
function download(name, text, type = 'text/csv;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function csv() {
  const ch = $('history-channel-select')?.value || channel;
  const metric = $('history-metric-select')?.value || 'Velocity';
  const data = bank.channels[ch] || bank.channels['1'];
  const metadata = data.metadata[metric];
  const rows = ['data_source,capture_time,channel,metric,value,unit,filter,detection'];
  data.times.forEach((time, i) => rows.push(['synthetic_demo', time, ch, metric, data.history[metric][i], metadata.unit, metadata.filter, metadata.detection].join(',')));
  return rows.join('\r\n') + '\r\n';
}
function readonly() {
  main.querySelectorAll('button,select,input,textarea').forEach(control => {
    if (control.tagName === 'INPUT' && !['checkbox', 'radio', 'submit', 'button'].includes(control.type)) control.readOnly = true;
    else control.disabled = true;
    control.title = 'Viewer preview — changes and service controls are disabled';
  });
  main.querySelectorAll('a[href^="/"]').forEach(anchor => {
    anchor.removeAttribute('href'); anchor.setAttribute('aria-disabled', 'true');
    anchor.classList.add('disabled'); anchor.title = 'Unavailable in the public viewer demo';
  });
  main.querySelectorAll('form').forEach(form => form.addEventListener('submit', event => event.preventDefault()));
}
async function renderTrends() {
  const template = document.querySelector('.demo-trend-template');
  if (!template) return;
  const row = template.parentElement;
  for (const ch of ['1','2','3','4','RPM']) {
    let wrapper = row.querySelector(`[data-trend-channel="${ch}"]`);
    if (!wrapper) {
      wrapper = template.cloneNode(true); wrapper.classList.remove('demo-trend-template');
      wrapper.dataset.trendChannel = ch; row.append(wrapper);
      wrapper.querySelectorAll('[id]').forEach(element => {
        const id = pattern(element);
        if (id.type) element.id = JSON.stringify({...id, channel:ch});
        else element.removeAttribute('id');
      });
      wrapper.querySelector('.demo-plot').replaceChildren();
    }
    wrapper.hidden = !trends.has(ch);
    if (wrapper.hidden) continue;
    const select = wrapper.querySelector('select');
    if (ch === 'RPM') { select.hidden = true; await plot(wrapper.querySelector('.demo-plot'), bank.figures['trend:RPM']); continue; }
    const metric = selectedMetrics[ch] || 'Velocity';
    select.replaceChildren(...metrics.map(name => { const option=document.createElement('option');option.value=name;option.textContent=name;return option; }));
    select.value=metric;select.disabled=false;select.removeAttribute('title');
    select.onchange = () => { selectedMetrics[ch] = select.value; renderTrends(); };
    await plot(wrapper.querySelector('.demo-plot'), bank.figures[`trend:${ch}:${metric}`]);
  }
}
function setupDashboard() {
  const firstPlot = main.querySelector('.demo-plot');
  const template = firstPlot?.closest('.col-lg-6');
  if (template) { template.classList.add('demo-trend-template'); template.dataset.trendChannel='1'; }
  main.querySelectorAll('input').forEach(input => {
    const id = pattern(input);
    if (id.type === 'ch-trend-check' || id.type === 'rpm-trend-check') {
      const ch=id.channel || 'RPM';input.disabled=false;input.checked=trends.has(ch);input.title='Show or hide this sample trend';
      input.addEventListener('change',()=>{input.checked?trends.add(ch):trends.delete(ch);renderTrends();});
    }
  });
  main.querySelectorAll('button').forEach(button => {
    const id=pattern(button);
    if (id.type === 'ch-wf-btn' || id.type === 'ch-spec-btn') {
      button.disabled=false;button.title='Inspect the sample capture';
      button.addEventListener('click',()=>openModal(id.type==='ch-wf-btn'?'waveform':'spectrum',id.channel));
    }
    if (button.classList.contains('accordion-button')) {
      button.disabled=false;button.removeAttribute('title');
      button.addEventListener('click',()=>{
        const body=button.closest('.accordion-item').querySelector('.accordion-collapse');
        const open=button.getAttribute('aria-expanded')!=='true';
        body.classList.toggle('show',open);button.classList.toggle('collapsed',!open);button.setAttribute('aria-expanded',String(open));
        if(open)resize();
      });
    }
  });
  return renderTrends();
}
function closeModal() {
  const dialog=$('demo-graph-modal');
  if(!dialog)return;
  dialog.querySelectorAll('.js-plotly-plot').forEach(element=>window.Plotly.purge(element));
  dialog.remove();$('demo-backdrop')?.remove();document.body.classList.remove('modal-open');modalFocus?.focus();
}
async function openModal(type,ch) {
  modalFocus=document.activeElement;
  const dialog=document.createElement('div');dialog.id='demo-graph-modal';dialog.className='modal demo-modal';
  dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby','demo-modal-title');
  dialog.innerHTML='<div class="modal-dialog modal-xl"><div class="modal-content"><div class="modal-header"><h5 id="demo-modal-title" class="modal-title"></h5><button class="btn-close" type="button" aria-label="Close"></button></div><div class="modal-body"><div class="demo-plot"></div></div><div class="modal-footer"><button type="button" class="btn btn-primary">Close</button></div></div></div>';
  const backdrop=document.createElement('div');backdrop.id='demo-backdrop';backdrop.className='modal-backdrop show';
  document.body.append(backdrop,dialog);document.body.classList.add('modal-open');
  $('demo-modal-title').textContent=`${type==='waveform'?'Waveform':'Spectrum'} - ${bank.gateway} CH${ch}`;
  dialog.querySelectorAll('button').forEach(button=>button.addEventListener('click',closeModal));
  dialog.addEventListener('click',event=>{if(event.target===dialog)closeModal();});
  dialog.addEventListener('keydown',event=>{
    if(event.key==='Escape')closeModal();
    if(event.key==='Tab'){
      const buttons=[...dialog.querySelectorAll('button')];
      if(event.shiftKey && document.activeElement===buttons[0]){event.preventDefault();buttons.at(-1).focus();}
      else if(!event.shiftKey && document.activeElement===buttons.at(-1)){event.preventDefault();buttons[0].focus();}
    }
  });
  await plot(dialog.querySelector('.demo-plot'),bank.figures[`${type}:${ch}`]);
  dialog.querySelector('button').focus();
}
async function setupChannelPage(route) {
  const prefix={'/assets':'asset','/alarms':'rule'}[route];
  if(!prefix)return;
  options(prefix+'-channel',channelOptions(),channel);
  $(prefix+'-channel').addEventListener('change',()=>{channel=$(prefix+'-channel').value;update();});
  if(route==='/alarms') {
    options('rule-metric',metrics,'Velocity');$('rule-metric').addEventListener('change',update);
  }
  async function update() {
    const data=bank.channels[channel];
    if(route==='/assets') {
      for(const key of ['site','machine','location','direction']) $('asset-'+key).value=data.asset[key];
      $('asset-interval').value=data.asset.publish_interval;
    }
    if(route==='/alarms') {
      const metric=$('rule-metric').value,metadata=data.metadata[metric];
      $('rule-context').textContent=`Current settings: ${metadata.unit} | ${metadata.filter} | ${metadata.detection}. Thresholds use the displayed unit.`;
      $('rule-enabled').checked=channel==='1' && metric==='Velocity';
      $('rule-alert').value=channel==='1' && metric==='Velocity'?5:6;
    }
  }
  await update();
}
// ---------------------------------------------------------------- machine history (sample data)
function syncLabels(selector) {  // the exported page keeps the highlight of the first render
  main.querySelectorAll(`${selector} input`).forEach(input => input.nextElementSibling?.classList.toggle('active', input.checked));
}
function enableRadios(selector, value, onChange) {
  main.querySelectorAll(`${selector} input`).forEach(input => {
    input.disabled = false; input.removeAttribute('title');
    input.checked = input.value === value;
    input.addEventListener('change', () => { syncLabels(selector); if (input.checked) onChange(input.value); });
  });
  syncLabels(selector);
}
function selectHistoryChannel(ch) {
  channel = ch; historyMoment = null;
  main.querySelectorAll('#diag-axis input').forEach(input => { input.checked = input.value === ch; });
  syncLabels('#diag-axis');
  main.querySelectorAll('.tree-leaf').forEach(leaf => leaf.classList.toggle('active', pattern(leaf).ch === ch));
  renderMachineHistory();
}
async function setupMachineHistory() {
  options('diag-range', [{value: '1d', label: '1 day'}, {value: '7d', label: '1 week'}, {value: 'max', label: 'Max'}], '1d');
  enableRadios('#diag-axis', channel, value => selectHistoryChannel(value));
  main.querySelectorAll('.tree-leaf').forEach(leaf => {
    leaf.disabled = false; leaf.removeAttribute('title');
    leaf.addEventListener('click', () => selectHistoryChannel(pattern(leaf).ch));
  });
  main.querySelectorAll('.overall-cell').forEach(cell => {
    cell.disabled = false; cell.removeAttribute('title');
    cell.addEventListener('click', () => {
      const id = pattern(cell); historyMetrics.add(id.metric);
      main.querySelectorAll('#diag-metrics input').forEach(input => { input.checked = historyMetrics.has(input.value); });
      syncLabels('#diag-metrics'); selectHistoryChannel(id.ch);
    });
  });
  main.querySelectorAll('#diag-tools input').forEach(input => {
    input.disabled = false; input.removeAttribute('title'); input.checked = historyTools.has(input.value);
    input.addEventListener('change', () => { input.checked ? historyTools.add(input.value) : historyTools.delete(input.value); syncLabels('#diag-tools'); renderCapture(); });
  });
  syncLabels('#diag-tools');
  const speed = $('diag-speed');
  if (speed) {
    speed.readOnly = false; speed.removeAttribute('title');
    speed.addEventListener('change', () => { historySpeed = Number(speed.value) > 0 ? Number(speed.value) : null; renderCapture(); });
  }
  enableRadios('#diag-speed-unit', 'rpm', () => renderCapture());
  enableRadios('#diag-view', historyView, value => { historyView = value; renderCapture(); });
  enableRadios('#diag-quantity', historyQuantity, value => { historyQuantity = value; renderCapture(); });
  main.querySelectorAll('#diag-metrics input').forEach(input => {
    input.disabled = false; input.removeAttribute('title'); input.checked = historyMetrics.has(input.value);
    input.addEventListener('change', () => { input.checked ? historyMetrics.add(input.value) : historyMetrics.delete(input.value); syncLabels('#diag-metrics'); renderMachineHistory(); });
  });
  syncLabels('#diag-metrics');
  await renderMachineHistory();
}
function nearestCapture(data) {
  if (!historyMoment) return data.captures.at(-1);
  const target = Date.parse(String(historyMoment).replace(' ', 'T'));
  return data.captures.reduce((best, item) => Math.abs(Date.parse(item.label) - target) < Math.abs(Date.parse(best.label) - target) ? item : best);
}
async function renderMachineHistory() {
  const revision = ++historyVersion, data = bank.channels[channel];
  const names = [...historyMetrics].filter(name => data.history[name]);
  const traces = names.map((name, i) => ({x: data.times, y: data.history[name], name: chips[name][0], yaxis: i ? 'y2' : 'y',
    type: 'scatter', mode: 'lines', line: {color: chips[name][1], width: 1.6},
    hovertemplate: `${chips[name][0]}: %{y:.4g} ${data.metadata[name]?.unit || ''}<extra></extra>`}));
  traces.push({x: data.captures.map(item => item.label), y: data.captures.map(() => 0.5), yaxis: 'y3', name: 'Saved spectra',
    type: 'scatter', mode: 'markers', marker: {symbol: 'line-ns-open', size: 12, color: '#1f3b73', line: {width: 2}}, hovertemplate: 'Saved spectrum<extra></extra>'});
  const axis = name => ({title: {text: `${chips[name][0]} ${data.metadata[name]?.unit || ''}`, font: {color: chips[name][1]}}, tickfont: {color: chips[name][1]}, domain: [0, 1]});
  const layout = {template: 'plotly_white', height: 360, margin: {l: 60, r: 60, t: 30, b: 40}, hovermode: 'x unified', showlegend: false,
    xaxis: {type: 'date', rangeslider: {visible: true, thickness: 0.07, bgcolor: '#f1f5f9'}}, yaxis: names[0] ? axis(names[0]) : {domain: [0, 1]},
    yaxis3: {domain: [0, 0.06], visible: false, range: [0, 1]}, shapes: [], annotations: []};
  if (names[1]) layout.yaxis2 = {...axis(names[1]), overlaying: 'y', side: 'right', showgrid: false};
  const capture = nearestCapture(data);
  if (historyMoment) {
    layout.shapes.push({type: 'line', x0: historyMoment, x1: historyMoment, y0: 0, y1: 1, yref: 'paper', line: {color: '#126b82', dash: 'dash'}});
  }
  const graph = document.querySelector('#diag-trend .demo-plot');
  await plot(graph, {data: traces, layout});
  if (revision !== historyVersion) return;
  graph.removeAllListeners?.('plotly_click');
  graph.on('plotly_click', event => { historyMoment = event.points[0].x; renderMachineHistory(); });
  await renderCapture(capture);
}
async function renderCapture(capture) {
  const data = bank.channels[channel];
  capture = capture || nearestCapture(data);
  const graph = document.querySelector('#diag-plot .demo-plot');
  const info = $('diag-capture-info');
  info.replaceChildren();
  const strong = document.createElement('strong'); strong.textContent = capture.label.replace('T', ' ').slice(0, 19);
  const note = document.createElement('span'); note.className = 'text-muted'; note.textContent = ' · sample capture';
  info.append(strong, note);
  if (historyMoment) {
    const minutes = Math.round(Math.abs(Date.parse(capture.label) - Date.parse(String(historyMoment).replace(' ', 'T'))) / 60000);
    const line = document.createElement('div'); line.className = 'small text-muted';
    line.textContent = minutes ? `Nearest saved capture: ${minutes} minute${minutes === 1 ? '' : 's'} from the moment you clicked.` : 'Saved at the moment you clicked.';
    info.append(line);
  }
  const peaks = $('diag-peaks'); if (peaks) peaks.replaceChildren();
  if (historyView === 'waterfall') return plot(graph, bank.figures[`waterfall:${channel}`]);
  if (historyView === 'waveform') {
    const wave = capture.waveform;
    return plot(graph, {data: [{x: wave.waveform_data.map((_, i) => i / wave.sampling_freq), y: wave.waveform_data, type: 'scatter', mode: 'lines', line: {color: '#1f3b73', width: 1}}],
      layout: {template: 'plotly_white', title: {text: 'Waveform'}, height: 460, xaxis: {title: {text: 'Time (s)'}}, yaxis: {title: {text: 'Waveform (sensor units)'}}}});
  }
  if (historyQuantity === 'envelope') {
    return plot(graph, {data: [], layout: {template: 'plotly_white', height: 460, xaxis: {visible: false}, yaxis: {visible: false},
      annotations: [{text: 'The envelope spectrum is calculated by the dashboard from each waveform; the public demo shows acceleration and velocity.', showarrow: false, xref: 'paper', yref: 'paper', x: 0.5, y: 0.5}]}});
  }
  const hz = capture.spectrum_data.map((_, i) => i * capture.bandwidth / capture.lines);
  let y = capture.spectrum_data;
  if (historyQuantity === 'velocity') y = y.map((p, i) => hz[i] >= 2 ? Math.sqrt(Math.abs(p)) * 9806.65 / (2 * Math.PI * hz[i]) : 0);
  const unit = main.querySelector('#diag-speed-unit input:checked')?.value || 'rpm';
  const rpm = historySpeed ? historySpeed * (unit === 'hz' ? 60 : 1) : (capture.rpm || bank.rpm);
  const speedInput = $('diag-speed'); if (speedInput) speedInput.placeholder = `auto: ${capture.rpm || bank.rpm} RPM`;
  const top = hz.at(-1), step = top / 10, ticks = Array.from({length: 11}, (_, i) => i * step);
  const traces = [{x: hz, y, type: 'scatter', mode: 'lines', line: {color: '#1f77b4', width: 1.3}, hovertemplate: '%{x:.2f} Hz<br>%{y:.4g}<extra></extra>'}];
  const shapes = [], annotations = [];
  if (historyTools.has('harmonics') && rpm > 0) {
    const harmonics = Array.from({length: 20}, (_, i) => (i + 1) * rpm / 60).filter(f => f <= top);
    harmonics.forEach(f => shapes.push({type: 'line', x0: f, x1: f, y0: 0, y1: 1, yref: 'paper', line: {dash: 'dash', color: '#2647d6', width: 1}}));
    const nearest = f => y[Math.round(f / (capture.bandwidth / capture.lines))] ?? 0;
    traces.push({x: harmonics, y: harmonics.map(nearest), type: 'scatter', mode: 'markers', name: 'Harmonics', marker: {symbol: 'diamond', size: 9, color: '#e3262f', line: {color: 'white', width: 1}}, hovertemplate: '%{x:.2f} Hz<extra></extra>'});
    annotations.push({x: harmonics[0], y: 1, yref: 'paper', text: `<b>${Math.round(rpm)} CPM</b><br>1x`, showarrow: false, xanchor: 'left', yanchor: 'top', align: 'left'});
  }
  await plot(graph, {data: traces, layout: {template: 'plotly_white', height: 470, showlegend: false, margin: {l: 60, r: 20, t: 20, b: 60}, shapes, annotations,
      xaxis: historyTools.has('logx') ? {type: 'log', title: {text: 'Frequency (Hz)'}} : {title: {text: 'f [Hz + CPM]'}, tickmode: 'array', tickvals: ticks, ticktext: ticks.map(t => `${Math.round(t)}<br>${Math.round(t * 60).toLocaleString('en')}`)},
      yaxis: {type: historyTools.has('logy') ? 'log' : 'linear', title: {text: historyQuantity === 'velocity' ? 'Velocity (mm/s, derived from the g² spectrum)' : 'Acceleration (published power spectrum, device units)'}}}});
  if (!peaks || !historyTools.has('peaks')) return;
  const found = [];
  const floor = 0.03 * Math.max(...y);  // real peaks only, not noise bumps
  for (let i = 1; i < y.length - 1; i++) if (y[i] > floor && y[i] > y[i - 1] && y[i] >= y[i + 1]) found.push(i);
  found.sort((a, b) => y[b] - y[a]);
  const table = document.createElement('table'); table.className = 'table table-sm table-striped mt-2';
  const head = table.createTHead().insertRow();
  for (const label of ['Peak (Hz)', 'x running speed', 'Amplitude']) { const th = document.createElement('th'); th.textContent = label; head.append(th); }
  const body = table.createTBody();
  for (const i of found.slice(0, 8)) { const row = body.insertRow(); for (const value of [hz[i].toFixed(2), (hz[i] / (rpm / 60)).toFixed(2), y[i].toPrecision(4)]) row.insertCell().textContent = value; }
  peaks.append(table);
}

async function setupHistory() {
  options('history-gateway-select',[bank.gateway],bank.gateway);
  options('history-channel-select',['1','2','3','4'],channel);
  options('history-metric-select',metrics,'Velocity');
  const load=async()=>{channel=$('history-channel-select').value;await plot(document.querySelector('#history-graph .demo-plot'),bank.figures[`history:${channel}:${$('history-metric-select').value}`]);};
  $('load-history-btn').disabled=false;$('load-history-btn').onclick=load;
  $('download-history-csv-btn').disabled=false;
  $('download-history-csv-btn').onclick=()=>download('vds-synthetic-sample.csv',csv());
  await load();
}
async function navigate() {
  const version = ++navigationVersion;
  delete document.body.dataset.demoReady;
  closeModal();
  main.querySelectorAll('.js-plotly-plot').forEach(element=>window.Plotly.purge(element));
  const hash=location.hash.slice(1);
  const aliases={overview:'/',diagnostics:'/diagnostics',alarms:'/alarms',guide:'/device'};
  const route=Object.hasOwn(pages,hash)?hash:(Object.hasOwn(aliases,hash)?aliases[hash]:'/');
  const page=pages[route];
  main.innerHTML='<div class="alert alert-info demo-notice" role="note"><strong>Public demo · Synthetic data</strong><span>Actual application layout. Captured sample, not live. Changes and service controls are disabled.</span><a href="https://github.com/dineshpalsingh1989/VDS-to-Dashboard">Source & installation ↗</a></div><div class="demo-page"></div><footer class="demo-footer">Viewer preview · No broker, database service, or equipment connection. Example thresholds are not equipment recommendations.</footer>';
  main.querySelector('.demo-page').innerHTML=page.html;
  readonly();
  document.querySelectorAll('#sidebar a').forEach(link=>{
    const active=link.getAttribute('href')==='#'+route;
    link.classList.toggle('active',active);active?link.setAttribute('aria-current','page'):link.removeAttribute('aria-current');
  });
  const active=document.querySelector('#sidebar [aria-current=page]');
  document.title=`${active?.textContent.trim() || 'Dashboard'} · VDS-to-Dashboard public demo`;
  await Promise.all(Array.from(main.querySelectorAll('[data-demo-plot]')).map(element=>plot(element,page.figures[Number(element.dataset.demoPlot)])));
  if(version!==navigationVersion)return;
  if(route==='/')await setupDashboard();
  await setupChannelPage(route);
  if(route==='/history')await setupHistory();
  if(route==='/diagnostics')await setupMachineHistory();
  if($('mqtt-status-bar'))$('mqtt-status-bar').textContent='Sample settings only — no MQTT broker is connected to this public demo.';
  document.body.dataset.demoReady=route;
}
window.addEventListener('hashchange',navigate);
await navigate();
