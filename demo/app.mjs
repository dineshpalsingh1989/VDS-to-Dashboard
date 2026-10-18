import { channels, scenarios, snapshot, spectrum, replayTime, events, csv } from './data.mjs';

const $ = id => document.getElementById(id);
let scenario = 'normal';
let tick = 0;
let selected = 1;
let paused = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const titles = { overview: 'Pump overview', diagnostics: 'Vibration diagnostics', alarms: 'Alarm events', guide: 'How it works' };
const subtitles = { overview: 'Four measurement points. One view of your machine.', diagnostics: 'Compare sample captures with their measurement context.', alarms: 'Follow illustrative events across sample scenarios.', guide: 'A quick tour of the monitor and its public demo.' };
const colors = { teal: '#087f8c', amber: '#b17b26', grey: '#9bacb8', grid: '#e8eef3', text: '#667b8f' };
function linePath(points, x, y) {
  let penDown = false;
  return points.map(p => {
    if (p.value === null) { penDown = false; return ''; }
    const command = penDown ? 'L' : 'M'; penDown = true;
    return `${command}${x(p).toFixed(2)},${y(p.value).toFixed(2)}`;
  }).join(' ');
}
function chart({ title, description, points, baseline, maxX, maxY, xLabel, yLabel, warning, ticks, harmonics = false }) {
  const w = 1000, h = 265, left = 63, right = 22, top = 24, bottom = 43;
  const x = value => left + value / maxX * (w - left - right);
  const y = value => h - bottom - value / maxY * (h - top - bottom);
  let markup = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${title}"><title>${title}</title><desc>${description}</desc>`;
  for (let i = 0; i <= 4; i++) {
    const v = i / 4 * maxY;
    markup += `<line x1="${left}" y1="${y(v)}" x2="${w-right}" y2="${y(v)}" stroke="${colors.grid}"/><text x="${left-12}" y="${y(v)+4}" text-anchor="end" fill="${colors.text}" font-size="11">${v.toFixed(maxY < 1 ? 2 : 1)}</text>`;
  }
  ticks.forEach(t => { markup += `<text x="${x(t.value)}" y="${h-22}" text-anchor="middle" fill="${colors.text}" font-size="11">${t.label}</text>`; });
  if (warning) markup += `<line x1="${left}" x2="${w-right}" y1="${y(warning)}" y2="${y(warning)}" stroke="${colors.amber}" stroke-dasharray="6 5"/><text x="${w-right}" y="${y(warning)-6}" text-anchor="end" fill="${colors.amber}" font-size="10">Example warning</text>`;
  if (harmonics) for (let n = 1; n <= 3; n++) {
    const hz = n * 1480 / 60;
    markup += `<line x1="${x(hz)}" x2="${x(hz)}" y1="${top}" y2="${h-bottom}" stroke="#d7e1e8" stroke-dasharray="3 4"/><text x="${x(hz)+4}" y="${top+9}" fill="${colors.text}" font-size="9">${n}×</text>`;
  }
  if (baseline) markup += `<path d="${linePath(baseline, p => x(p.x), y)}" fill="none" stroke="${colors.grey}" stroke-width="1.6" stroke-dasharray="5 4"/>`;
  markup += `<path d="${linePath(points, p => x(p.x), y)}" fill="none" stroke="${colors.teal}" stroke-width="2.4" stroke-linejoin="round"/><text x="${left}" y="12" fill="${colors.text}" font-size="10">${yLabel}</text><text x="${w-right}" y="${h-4}" text-anchor="end" fill="${colors.text}" font-size="10">${xLabel}</text></svg>`;
  return markup;
}
function renderChannels(data) {
  const container = $('channels');
  data.forEach(channel => {
    let button = document.querySelector(`#channels [data-channel="${channel.id}"]`);
    if (!button) {
      button = document.createElement('button');
      button.type = 'button'; button.className = 'channel-card'; button.dataset.channel = channel.id;
      button.addEventListener('click', () => { selected = channel.id; $('diag-channel').value = selected; render(); });
      container.append(button);
    }
    const status = channel.stale ? 'Stale' : channel.warning ? 'Warning' : 'Normal';
    button.setAttribute('aria-pressed', String(selected === channel.id));
    button.setAttribute('aria-label', `CH${channel.id}, ${channel.name}, ${status}, ${channel.value.toFixed(2)} millimeters per second RMS${channel.stale ? ', last known value' : ''}`);
    const history = channel.history.slice(-24);
    const spark = linePath(history, p => (p.minute - history[0].minute) / 23 * 200, v => 35 - v / 7 * 33);
    button.innerHTML = `<span class="channel-top"><span>CHANNEL ${channel.id}</span><span class="pill ${channel.stale ? 'stale' : channel.warning ? 'warning' : ''}">${status}</span></span><h3>${channel.name}</h3><span class="direction">${channel.direction}</span><strong class="reading">${channel.value.toFixed(2)} <small>mm/s RMS</small></strong><svg class="sparkline" viewBox="0 0 200 40" preserveAspectRatio="none" aria-hidden="true"><path d="${spark}" fill="none" stroke="${channel.stale ? colors.grey : channel.warning ? colors.amber : colors.teal}" stroke-width="2"/></svg><span class="channel-meta"><span>${channel.acceleration.toFixed(2)} g RMS · reference</span><span>${channel.stale ? `Last: ${channel.ageMinutes} min ago` : 'Sample capture'}</span></span>`;
  });
}
function render() {
  const data = snapshot(scenario, tick), active = data.find(c => c.id === selected);
  $('scenario-description').textContent = scenarios[scenario];
  $('asset-condition').textContent = scenario === 'missing' ? 'Data incomplete' : data.some(c => c.warning) ? 'Warning' : 'Normal';
  $('condition-detail').textContent = scenario === 'missing' ? 'CH3 is stale · inspect last capture' : scenario === 'rising' ? 'CH1 exceeds an example threshold' : 'Within example thresholds';
  $('reporting').innerHTML = `${data.filter(c => !c.stale).length} <em>/ 4</em>`;
  $('capture-time').textContent = replayTime(59 + tick);
  renderChannels(data);
  const history = active.history.slice(-Number($('window').value));
  const start = history[0].minute, range = history.length - 1;
  $('trend-title').textContent = `CH${active.id} · ${active.name} · Velocity trend${active.stale ? ' (stale)' : ''}`;
  $('trend-chart').innerHTML = chart({ title: `CH${active.id} sample velocity trend`, description: `Synthetic RMS velocity in mm/s. ${active.stale ? 'Five minutes of missing readings are left blank.' : 'A dashed line shows the illustrative 4.5 mm/s warning level.'}`, points: history.map(p => ({ x: p.minute - start, value: p.value })), maxX: range, maxY: 7, xLabel: 'Replay time (UTC)', yLabel: 'mm/s RMS', warning: 4.5, ticks: Array.from({length: 5}, (_,i) => ({value: i/4*range, label: replayTime(start + Math.round(i/4*range)).slice(0,5)})) });
  const spec = spectrum(active, scenario), base = $('baseline').checked ? spectrum(active, scenario, true) : null;
  $('spectrum-chart').innerHTML = chart({ title: `CH${active.id} synthetic spectrum${active.stale ? ', last known capture' : ''}`, description: 'Illustrative acceleration amplitude with harmonic markers and an optional example baseline. No machine fault diagnosis is made.', points: spec.map(p => ({x:p.hz,value:p.value})), baseline: base?.map(p => ({x:p.hz,value:p.value})), maxX:500,maxY:0.32,xLabel:$('orders').checked ? 'Order (frequency / running speed)' : 'Frequency (Hz)', yLabel:'Amplitude (g)',harmonics:true,ticks:Array.from({length:6},(_,i)=>({value:i*100,label:$('orders').checked ? (i*100/(1480/60)).toFixed(1)+'×' : String(i*100)})) });
  // DOM text nodes keep future sample labels out of HTML parsing.
  $('event-rows').replaceChildren(...events(scenario, tick).map(event => {
    const tr = document.createElement('tr');
    for (const value of [replayTime(event.minute), event.channel, event.state, event.message]) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    return tr;
  }));
  $('pause').textContent = paused ? 'Resume replay' : 'Pause replay';
  $('replay-status').textContent = paused ? 'Sample replay paused' : 'Sample replay running';
}
function navigate() {
  const view = Object.hasOwn(titles, location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  document.querySelectorAll('[data-panel]').forEach(panel => panel.hidden = panel.dataset.panel !== view);
  document.querySelectorAll('[data-view]').forEach(link => link.dataset.view === view ? link.setAttribute('aria-current', 'page') : link.removeAttribute('aria-current'));
  $('page-title').textContent = titles[view];
  document.querySelector('.subtitle').textContent = subtitles[view];
  document.title = `${titles[view]} · Wilcoxon VDS demo`;
}
for (const channel of channels) {
  const option = document.createElement('option'); option.value = channel.id; option.textContent = `CH${channel.id} · ${channel.name}`; $('diag-channel').append(option);
}
$('scenario').addEventListener('change', event => { scenario = event.target.value; tick = 0; render(); });
$('diag-channel').addEventListener('change', event => { selected = Number(event.target.value); render(); });
for (const id of ['window','baseline','orders']) $(id).addEventListener('change', render);
$('pause').addEventListener('click', () => { paused = !paused; render(); });
$('reset').addEventListener('click', () => { tick = 0; scenario = 'normal'; selected = 1; $('scenario').value = 'normal'; $('diag-channel').value = '1'; render(); });
$('export').addEventListener('click', () => {
  const channel = snapshot(scenario, tick).find(c => c.id === selected);
  const url = URL.createObjectURL(new Blob([csv(channel)], {type:'text/csv;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = `synthetic-demo-ch${selected}-${scenario}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener('hashchange', navigate);
setInterval(() => { if (!paused && !document.hidden) { tick++; render(); } }, 3000);
navigate(); render();
