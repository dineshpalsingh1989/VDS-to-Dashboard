// Export rendered application screens. No hand-maintained duplicate dashboard.
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { basename, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { openBrowser, wait } from './demo-browser.mjs';

const root=fileURLToPath(new URL('../',import.meta.url));
const python=process.env.DEMO_PYTHON || (process.platform==='win32' ? `${root}.venv\\Scripts\\python.exe` : 'python');
await promisify(execFile)(python,['scripts/demo-assets.py'],{cwd:root,windowsHide:true});
let fixture, browser, fixtureRoot;
try {
  let source=process.env.DEMO_SOURCE_URL;
  if(!source) {
    fixtureRoot=await mkdtemp(join(tmpdir(),'vds-demo-export-'));
    fixture=spawn(python,['scripts/demo-fixture.py'],{cwd:root,windowsHide:true,
      env:{...process.env,VDS_DEMO_EXPORT_ROOT:fixtureRoot},stdio:['ignore','pipe','pipe']});
    source=await new Promise((resolve,reject)=>{
      let output='',errors='';
      const timeout=setTimeout(()=>reject(new Error('Demo fixture startup timed out: '+errors.slice(-1500))),45000);
      fixture.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/DEMO_URL=(http:\/\/127\.0\.0\.1:\d+)/);if(match){clearTimeout(timeout);resolve(match[1]);}});
      fixture.stderr.on('data',chunk=>{errors+=chunk;});
      fixture.on('error',error=>{clearTimeout(timeout);reject(error);});
      fixture.on('exit',code=>{clearTimeout(timeout);reject(new Error(`Demo fixture exited (${code}): ${errors.slice(-1500)}`));});
    });
  }
  const bank=await (await fetch(source+'/__demo__/bank')).json();
  const exported={bank,pages:{},shell:{},source:'Rendered app.py and workspace_views.py with synthetic data only',layoutSources:{}};
  for(const path of ['app.py','workspace_views.py','assets/dashboard.css']) {
    const content=(await readFile(new URL('../'+path,import.meta.url),'utf8')).replaceAll('\r\n','\n');
    exported.layoutSources[path]=createHash('sha256').update(content).digest('hex');
  }
  browser=await openBrowser();
  const {call,evaluate,until}=browser;
  const styles=new Set();
  async function navigate(path,ready) {
    await call('Page.navigate',{url:source+path});
    await until(`Boolean(document.querySelector(${JSON.stringify(ready)})) && Boolean(window.dash_clientside)`);
    await evaluate(`['live-refresh','system_info_interval','file_update_interval','job_update_interval','mqtt-status-interval'].forEach(id=>{if(document.getElementById(id))window.dash_clientside.set_props(id,{disabled:true})})`);
    await until('document.querySelectorAll("[data-dash-is-loading=true]").length===0');
    await wait(250);
  }
  async function choose(id) {
    await evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
    await until('Boolean(document.querySelector("[role=option]"))');
    await evaluate('document.querySelector("[role=option]").click()');
    await until('document.querySelectorAll("[data-dash-is-loading=true]").length===0');
    await wait(200);
  }
  const routes=[['/','#device-dashboard-container'],['/health','#health-findings'],['/assets','#asset-channel'],
    ['/alarms','#rule-channel'],['/diagnostics','#diag-channel'],['/reports','#report-preview'],
    ['/device','#device-info-list'],['/data-storage','#data-storage-controls-container'],['/history','#history-channel-select'],
    ['/debug','#debug-log-output'],['/mqtt','#mqtt-host-input'],['/ethernet','#ethernet-settings-card-body'],
    ['/modbus','#modbus-table-container'],['/opcua','#opc-live-tree-container']];
  for(const [path,ready] of routes) {
    await navigate(path,ready);
    if(path==='/') {
      await until('Boolean(document.querySelector("#device-accordion .accordion-button"))');
      await evaluate('document.querySelector("#device-accordion .accordion-button").click()');
      await until('document.querySelectorAll("#device-accordion .js-plotly-plot").length > 0');
    }
    if(path==='/assets') await choose('asset-channel');
    if(path==='/alarms') await choose('rule-channel');
    if(path==='/diagnostics') {
      await choose('diag-channel');
      await until('document.querySelector("#diag-spectrum .js-plotly-plot")?._fullData?.[0]?.x?.length > 0');
    }
    if(path==='/history') {
      await evaluate('document.getElementById("load-history-btn").click()');
      await until('document.querySelector("#history-graph .js-plotly-plot")?.data?.length > 0');
    }
    const page=await evaluate(`(() => {
      const original=document.getElementById('page-content'),copy=original.cloneNode(true),figures=[];
      const originalPlots=original.querySelectorAll('.js-plotly-plot'),clonedPlots=copy.querySelectorAll('.js-plotly-plot');
      originalPlots.forEach((plot,index)=>{
        const layout=JSON.parse(JSON.stringify(plot.layout));delete layout.width;layout.autosize=true;
        figures.push({data:JSON.parse(JSON.stringify(plot.data)),layout});
        const placeholder=document.createElement('div');placeholder.dataset.demoPlot=index;placeholder.className='demo-plot';
        clonedPlots[index].replaceWith(placeholder);
      });
      original.querySelectorAll('input,textarea,select').forEach((input,index)=>{
        const clone=copy.querySelectorAll('input,textarea,select')[index];
        if(input.tagName==='TEXTAREA')clone.textContent=input.value;
        else clone.setAttribute('value',input.value);
        if(input.checked)clone.setAttribute('checked','');else clone.removeAttribute('checked');
      });
      copy.querySelectorAll('.dash-dropdown').forEach(dropdown=>{
        const select=document.createElement('select');select.id=dropdown.id;select.className='form-select '+dropdown.className.replace('dash-dropdown','');
        const option=document.createElement('option');option.textContent=dropdown.querySelector('.Select-value-label,.Select-placeholder')?.textContent||'Select…';
        select.append(option);dropdown.replaceWith(select);
      });
      copy.querySelectorAll('script').forEach(node=>node.remove());
      copy.querySelectorAll('[data-dash-is-loading]').forEach(node=>node.removeAttribute('data-dash-is-loading'));
      // React permits nested buttons in the app's accordion header. HTML parsing
      // would close the outer button early and move its cards outside the item.
      copy.querySelectorAll('.accordion-button button').forEach(button=>{
        const span=document.createElement('span');
        span.className=button.className+' disabled';span.innerHTML=button.innerHTML;
        span.setAttribute('aria-disabled','true');span.title='Disabled in the viewer preview';
        button.replaceWith(span);
      });
      return {html:copy.innerHTML,figures};
    })()`);
    exported.pages[path]=page;
    for(const text of await evaluate(`Array.from(document.querySelectorAll('style')).map(style=>{try{return Array.from(style.sheet?.cssRules||[]).map(rule=>rule.cssText).join('\\n')}catch{return style.textContent}})`)) if(text)styles.add(text);
    if(path==='/') exported.shell=await evaluate('({header:document.querySelector("nav.navbar").outerHTML,sidebar:document.getElementById("sidebar").outerHTML})');
    console.log('Exported '+path);
  }
  await writeFile(new URL('../demo/snapshots.json',import.meta.url),JSON.stringify(exported));
  await writeFile(new URL('../demo/vendor/components.css',import.meta.url),Array.from(styles).join('\n'));
  console.log(`Exported ${routes.length} real application pages with synthetic data.`);
} finally {
  await browser?.close();
  if(fixture && fixture.exitCode===null) {
    const exited=new Promise(resolve=>fixture.once('exit',resolve));
    fixture.kill();await exited;
  }
  if(fixtureRoot) {
    if(!resolve(fixtureRoot).startsWith(resolve(tmpdir())+sep) || !basename(fixtureRoot).startsWith('vds-demo-export-'))throw new Error('Unexpected fixture cleanup path');
    await rm(fixtureRoot,{recursive:true,force:true,maxRetries:10,retryDelay:200});
  }
}
