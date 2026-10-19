import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { openBrowser, wait } from './demo-browser.mjs';

const root=fileURLToPath(new URL('../demo/',import.meta.url));
const prefix='/Wilcoxon-VDS/';
const types={'.html':'text/html','.mjs':'text/javascript','.js':'text/javascript','.json':'application/json','.css':'text/css','.woff2':'font/woff2','.ttf':'font/ttf'};
const server=createServer(async(request,response)=>{
  const pathname=new URL(request.url,'http://localhost').pathname;
  if(!pathname.startsWith(prefix)){response.writeHead(404).end();return;}
  const path=resolve(root,decodeURIComponent(pathname.slice(prefix.length))||'index.html');
  if(!path.startsWith(resolve(root)+sep)){response.writeHead(404).end();return;}
  try{const content=await readFile(path);response.writeHead(200,{'Content-Type':types[extname(path)]||'application/octet-stream'});response.end(content);}
  catch{response.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const url=process.env.DEMO_URL || `http://127.0.0.1:${server.address().port}${prefix}`;
let browser;
try {
  browser=await openBrowser();
  const {call,evaluate,until}=browser;
  await call('Page.navigate',{url});
  await until('document.body.dataset.demoReady === "/"');
  assert.equal(await evaluate('document.querySelectorAll("#sidebar a").length'),14);
  assert.equal(await evaluate('document.querySelector("nav.navbar").textContent.trim()'),'VDS Condition Monitor');
  assert((await evaluate('document.querySelector("#device-accordion").textContent')).includes('Std Deviation:'));
  assert.equal(await evaluate('getComputedStyle(document.querySelector("#sidebar")).width'),'288px');
  if(process.env.DEMO_SCREENSHOT) {
    const image=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
    await writeFile(process.env.DEMO_SCREENSHOT,Buffer.from(image.data,'base64'));
  }
  // Real dashboard trend switches and metric selectors remain exploratory.
  await evaluate(`Array.from(document.querySelectorAll('input')).find(el=>{try{const id=JSON.parse(el.id);return id.type==='ch-trend-check'&&id.channel==='2'}catch{return false}}).click()`);
  await until(`Boolean(document.querySelector('[data-trend-channel="2"] .js-plotly-plot'))`);
  await evaluate(`const select=document.querySelector('[data-trend-channel="2"] select');select.value='Acceleration';select.dispatchEvent(new Event('change'))`);
  await until(`document.querySelector('[data-trend-channel="2"] .js-plotly-plot')?.layout?.title?.text === 'CH2 - Acceleration'`);
  await evaluate(`Array.from(document.querySelectorAll('button')).find(el=>{try{return JSON.parse(el.id).type==='ch-wf-btn'}catch{return false}}).click()`);
  await until('Boolean(document.querySelector("#demo-graph-modal .js-plotly-plot"))');
  assert((await evaluate('document.querySelector("#demo-modal-title").textContent')).includes('Waveform'));
  await evaluate('document.querySelector("#demo-graph-modal .btn-close").click()');
  assert.equal(await evaluate('Boolean(document.querySelector("#demo-graph-modal"))'),false);
  const routes=['/health','/assets','/alarms','/diagnostics','/reports','/device','/data-storage','/history','/debug','/mqtt','/ethernet','/modbus','/opcua'];
  async function navigate(route) {
    await evaluate(`location.hash=${JSON.stringify(route)}`);
    try { await until(`document.body.dataset.demoReady === ${JSON.stringify(route)}`); }
    catch(error) { console.log("Browser errors:", browser.errors); throw error; }
  }
  for(const route of routes)await navigate(route);
  for(const [route,id] of [['/assets','asset-save'],['/alarms','rule-save'],['/diagnostics','baseline-save'],['/modbus','modbus-add-btn'],['/opcua','opc-server-switch']]) {
    await navigate(route);assert(await evaluate(`document.getElementById(${JSON.stringify(id)}).disabled`),id+' must be disabled');
  }
  await navigate('/diagnostics');
  await evaluate('document.querySelector("#diag-channel").value="2";document.querySelector("#diag-channel").dispatchEvent(new Event("change"))');
  await evaluate('document.querySelector("#diag-orders input").click()');
  await until('document.querySelector("#diag-spectrum .js-plotly-plot")?.layout?.xaxis?.title?.text?.startsWith("Order")');
  await until('document.querySelector("#diag-waveform .js-plotly-plot")?.data?.[0]?.y?.length > 0');
  await evaluate('document.querySelector("#diag-rpm").value="1200";document.querySelector("#diag-rpm").dispatchEvent(new Event("change"))');
  await until('document.querySelector("#diag-spectrum .js-plotly-plot")?.layout?.title?.text?.includes("1200")');
  await evaluate('const captures=document.querySelector("#diag-capture");captures.value="0";captures.dispatchEvent(new Event("change"))');
  await until('document.querySelector("#diag-capture").value === "0" && document.querySelector("#diag-peaks tbody tr")');
  assert((await evaluate('document.querySelector("#diag-details").textContent')).includes('Manual override'));
  await evaluate('document.querySelector("#diag-spectrum .js-plotly-plot").emit("plotly_click",{points:[{customdata:[24,1.2],y:.2}]})');
  assert((await evaluate('document.querySelector("#diag-cursor").textContent')).includes('24.000 Hz'));
  await until('document.querySelector("#diag-spectrum .js-plotly-plot").getBoundingClientRect().height >= 350');
  if(process.env.DEMO_DIAGNOSTICS_SCREENSHOT) {
    await wait(300);
    const image=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
    await writeFile(process.env.DEMO_DIAGNOSTICS_SCREENSHOT,Buffer.from(image.data,'base64'));
  }
  await navigate('/assets');
  assert.equal(await evaluate('document.querySelector("#asset-direction").value'),'Vertical');
  await navigate('/history');
  await call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:browser.profile});
  await evaluate('document.querySelector("#download-history-csv-btn").click()');
  let download;
  for(let i=0;i<40;i++){download=(await readdir(browser.profile)).find(file=>file.endsWith('.csv'));if(download)break;await wait(100);}
  assert(download,'CSV download must complete');
  assert((await readFile(resolve(browser.profile,download),'utf8')).includes('synthetic_demo'));
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate('document.body.classList.add("demo-nav-hidden")');
  await wait(400);
  for(const route of ['/',...routes]) {
    await navigate(route);
    const overflow=await evaluate('document.documentElement.scrollWidth > 390');
    if(overflow) console.log('Overflow details',route,await evaluate(`Array.from(document.querySelectorAll('body *')).map(el=>({tag:el.tagName,id:el.id,class:typeof el.className==='string'?el.className:'',right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width})).filter(el=>el.right>391).slice(0,12)`));
    assert(!overflow,`No horizontal page overflow: ${route}`);
  }
  if(process.env.DEMO_MOBILE_SCREENSHOT) {
    await navigate('/');
    const image=await call('Page.captureScreenshot',{format:'png'});
    await writeFile(process.env.DEMO_MOBILE_SCREENSHOT,Buffer.from(image.data,'base64'));
  }
  assert.deepEqual(browser.errors,[],'No browser errors or CSP violations');
  console.log('Passed: actual layout, 14 routes, trend switches, metric selection, waveform dialog, order axis, CSV, disabled actions, and mobile views.');
} finally {
  await browser?.close();await new Promise(resolve=>server.close(resolve));
}
