// Enhance exported real application screens. This file has no backend actions.
const response = await fetch(new URL('snapshots.json', import.meta.url), {cache: 'no-cache'});
if (!response.ok) throw new Error('Could not load the sample dashboard');
const { bank, pages, shell } = await response.json();
const $ = id => document.getElementById(id);
const metrics = ['Velocity', 'Acceleration', 'Crest Factor', 'Displacement', 'True Peak', 'Std Deviation'];
let channel = '1', modalFocus;
let orders = false;
let manualRpm = "", captureChoice = "live";
let navigationVersion = 0, diagnosticVersion = 0;
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
  const prefix={'/assets':'asset','/alarms':'rule','/diagnostics':'diag'}[route];
  if(!prefix)return;
  options(prefix+'-channel',channelOptions(),channel);
  $(prefix+'-channel').addEventListener('change',()=>{channel=$(prefix+'-channel').value;captureChoice='live';update();});
  if(route==='/alarms') {
    options('rule-metric',metrics,'Velocity');$('rule-metric').addEventListener('change',update);
  }
  if(route==='/diagnostics') {
    const input=document.querySelector('#diag-orders input');input.disabled=false;input.checked=orders;
    input.addEventListener('change',()=>{orders=input.checked;update();});
    const rpm=$('diag-rpm');rpm.readOnly=false;rpm.value=manualRpm;rpm.removeAttribute('title');
    rpm.addEventListener('change',()=>{manualRpm=rpm.value;update();});
    $('diag-capture').addEventListener('change',()=>{captureChoice=$('diag-capture').value;update();});
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
    if(route==='/diagnostics') {
      await renderDiagnostics(data);
      await plot(document.querySelector('#diag-waterfall .demo-plot'),bank.figures[`waterfall:${channel}`]);
    }
  }
  await update();
}
async function renderDiagnostics(data) {
  const revision=++diagnosticVersion, navigation=navigationVersion;
  const active=()=>revision===diagnosticVersion && navigation===navigationVersion;
  const captures=data.captures;
  options('diag-capture',[{label:'Live / latest sample',value:'live'},
    ...captures.map((item,i)=>({label:new Date(item.timestamp_ms).toISOString(),value:String(i)})).reverse()],captureChoice);
  const current=captures[captureChoice==='live'?captures.length-1:Number(captureChoice)];
  const automatic=captureChoice==='live'?bank.rpm:current.rpm;
  const speed=manualRpm===''?automatic:Number(manualRpm);
  const validManual=manualRpm==='' || (Number.isFinite(speed) && speed>0);
  const notices=[];
  $('diag-cursor').textContent='Click a spectrum point to inspect frequency, amplitude, and order.';
  const details=$('diag-details');details.replaceChildren();
  for(const [kind,item] of [['Waveform',current.waveform],['Spectrum',current]]) {
    const card=document.createElement('div');card.className='acquisition-card';
    const title=document.createElement('h6');title.textContent=kind;card.append(title);
    const dl=document.createElement('dl');card.append(dl);
    const values={'Capture (UTC)':new Date(item.timestamp_ms).toISOString(),
      'Amplitude':kind==='Spectrum'?'Device units':'Sensor units',
      'Sampling rate':`${item.sampling_freq} Hz`,
      'RPM':kind==='Spectrum'?(speed??'Unavailable'):(item.rpm??'Unavailable'),
      'RPM source':kind==='Spectrum' && manualRpm!==''?'Manual override':kind==='Spectrum' && captureChoice==='live'?'Dashboard':'Capture'};
    if(kind==='Spectrum')Object.assign(values,{'Frequency resolution':`${item.bandwidth/item.lines} Hz`,'Bandwidth':`${item.bandwidth} Hz`,'FFT window':item.window||'Unknown'});
    for(const [key,value] of Object.entries(values)) {
      const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
      dt.textContent=key;dd.textContent=String(value);row.append(dt,dd);dl.append(row);
    }
    details.append(card);
  }
  const wave=current.waveform;
  await plot(document.querySelector('#diag-waveform .demo-plot'),{data:[{x:wave.waveform_data.map((_,i)=>i/wave.sampling_freq),y:wave.waveform_data,type:'scattergl',mode:'lines'}],layout:{template:'plotly_white',title:'Time waveform',xaxis:{title:{text:'Time (s)'}},yaxis:{title:{text:'Published waveform (sensor units)'}}}});
  if(!active())return;
  const graph=document.querySelector('#diag-spectrum .demo-plot');
  const table=$('diag-peaks');table.replaceChildren();
  if(!validManual || (orders && !(speed>0))) {
    notices.push('Enter a finite, positive RPM for order analysis.');
    await plot(graph,{data:[],layout:{title:'Spectrum unavailable'}});
  } else {
    const fig=clone(bank.figures[`diagnostics:${channel}:0`]);
    fig.data=[current,data.baseline].map((item,index)=>{
      const rpm=manualRpm===''?(index?item.rpm:speed):speed;
      const hz=item.spectrum_data.map((_,i)=>i*item.bandwidth/item.lines);
      return {name:index?'Baseline':'Current',mode:'lines',type:'scatter',line:index?{dash:'dot'}:{},
        x:hz.map(v=>orders?v/(rpm/60):v),y:item.spectrum_data,
        customdata:hz.map(v=>[v,rpm>0?v/(rpm/60):null]),
        hovertemplate:'%{customdata[0]:.3f} Hz<br>%{y:.6g} device units<br>Order: %{customdata[1]:.3f}<extra>%{fullData.name}</extra>'};
    });
    fig.layout.xaxis.title.text=orders?'Order (x RPM)':'Frequency (Hz)';
    fig.layout.title.text=manualRpm===''?'Spectrum comparison':`Spectrum comparison - manual RPM: ${speed}`;
    fig.layout.shapes=[];fig.layout.annotations=[];
    if(speed>0)for(let n=1;n<=5;n++) {
      const position=orders?n:n*speed/60;
      if(position<=fig.data[0].x.at(-1))fig.layout.shapes.push({type:'line',x0:position,x1:position,y0:0,y1:1,yref:'paper',line:{dash:'dash',color:'#b36b00'}});
    }
    await plot(graph,fig);
    if(!active())return;
    graph.removeAllListeners?.('plotly_click');
    graph.on('plotly_click',event=>{
      const p=event.points[0],[hz,order]=p.customdata;
      $('diag-cursor').textContent=`Frequency: ${hz.toFixed(3)} Hz | Amplitude: ${Number(p.y).toPrecision(6)} device units | Order: ${order==null?'Unavailable':order.toFixed(3)}`;
    });
    const peaks=[];const y=current.spectrum_data;
    for(let i=1;i<y.length-1;i++)if(y[i]>y[i-1] && y[i]>=y[i+1])peaks.push({i,value:y[i]});
    peaks.sort((a,b)=>b.value-a.value || a.i-b.i);
    const native=document.createElement('table');native.className='table mb-0';
    const header=native.createTHead().insertRow();
    for(const label of ['Frequency (Hz)','Amplitude (device units)','Order']) {const th=document.createElement('th');th.textContent=label;header.append(th);}
    const body=native.createTBody();
    for(const peak of peaks.slice(0,10)) {
      const hz=peak.i*current.bandwidth/current.lines;
      const row=body.insertRow();
      for(const value of [hz.toFixed(3),peak.value.toPrecision(6),speed>0?(hz/(speed/60)).toFixed(3):'Unavailable'])row.insertCell().textContent=value;
    }
    table.append(native);
  }
  if(active())$('diag-result').textContent=notices.join(' ');
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
  main.innerHTML='<div class="alert alert-info demo-notice" role="note"><strong>Public demo · Synthetic data</strong><span>Actual application layout. Captured sample, not live. Changes and service controls are disabled.</span><a href="https://github.com/dineshpalsingh1989/Wilcoxon-VDS">Source & installation ↗</a></div><div class="demo-page"></div><footer class="demo-footer">Viewer preview · No broker, database service, or equipment connection. Example thresholds are not equipment recommendations.</footer>';
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
  if($('mqtt-status-bar'))$('mqtt-status-bar').textContent='Sample settings only — no MQTT broker is connected to this public demo.';
  document.body.dataset.demoReady=route;
}
window.addEventListener('hashchange',navigate);
await navigate();
