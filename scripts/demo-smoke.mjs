import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { openBrowser, wait } from './demo-browser.mjs';

const root=fileURLToPath(new URL('../demo/',import.meta.url));
const prefix='/VDS-to-Dashboard/';
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
  await until('document.body?.dataset.demoReady === "/"');
  assert.equal(await evaluate('document.querySelectorAll("#sidebar a").length'),8);
  assert.equal(await evaluate('document.querySelector("nav.navbar").textContent.trim()'),'VDS-to-Dashboard');
  assert((await evaluate('document.querySelector("#device-accordion").textContent')).includes('Std deviation'));
  assert.equal(await evaluate('getComputedStyle(document.querySelector("#sidebar")).width'),'288px');
  if(process.env.DEMO_SCREENSHOT) {
    const image=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
    await writeFile(process.env.DEMO_SCREENSHOT,Buffer.from(image.data,'base64'));
  }
  assert.equal(await evaluate('document.querySelectorAll("#device-accordion .fa-chevron-up, #device-accordion .fa-chevron-down").length'),0,'Use only the native accordion arrow');
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
    try { await until(`document.body?.dataset.demoReady === ${JSON.stringify(route)}`); }
    catch(error) { console.log("Browser errors:", browser.errors); throw error; }
  }
  for(const route of routes) {
    await navigate(route);
    const screenshot={'/assets':process.env.DEMO_ASSETS_SCREENSHOT,'/alarms':process.env.DEMO_ALARMS_SCREENSHOT}[route];
    if(screenshot) {
      const image=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
      await writeFile(screenshot,Buffer.from(image.data,'base64'));
    }
  }
  for(const [route,id] of [['/assets','asset-save'],['/alarms','rule-save'],['/diagnostics','baseline-save'],['/modbus','modbus-add-btn'],['/opcua','opc-server-switch']]) {
    await navigate(route);assert(await evaluate(`document.getElementById(${JSON.stringify(id)}).disabled`),id+' must be disabled');
  }
  await navigate('/mqtt');
  assert(await evaluate('document.querySelector("#nav-settings").classList.contains("active")'),'Settings menu entry is active on its tabs');
  await evaluate('document.querySelector(".settings-tab[href$=opcua]").click()');
  await until('document.body?.dataset.demoReady === "/opcua" && document.querySelector(".settings-tab.active").textContent.includes("OPC UA")');
  assert((await evaluate('document.querySelector("#pi-serial")?.id || ""'))==='', 'Device details are not on the OPC UA tab');
  await navigate('/diagnostics');
  await until('document.querySelector("#diag-trend .js-plotly-plot")?.data?.some(trace => trace.name === "Saved spectra")');
  await until('document.querySelector("#diag-plot .js-plotly-plot")?.layout?.xaxis?.title?.text === "f [Hz + CPM]"');
  await until('document.querySelector("#diag-plot .js-plotly-plot")?.data?.some(trace => trace.name === "Harmonics")');
  await evaluate('const p=document.querySelector("#diag-trend .js-plotly-plot");p.emit("plotly_click",{points:[{x:p.data[0].x[2]}]})');
  await until('document.querySelector("#diag-capture-info").textContent.includes("moment you clicked")');
  await until('document.querySelector("#diag-trend .js-plotly-plot")?.layout?.shapes?.length === 1');
  if(process.env.DEMO_DIAGNOSTICS_SCREENSHOT) {
    // Lay the page out at screenshot size first, so Plotly redraws at that width.
    await call('Emulation.setDeviceMetricsOverride',{width:1600,height:1150,deviceScaleFactor:1,mobile:false});
    await evaluate('window.dispatchEvent(new Event("resize"))');
    await wait(800);
    const image=await call('Page.captureScreenshot',{format:'png'});
    await writeFile(process.env.DEMO_DIAGNOSTICS_SCREENSHOT,Buffer.from(image.data,'base64'));
    await call('Emulation.clearDeviceMetricsOverride');
    await evaluate('window.dispatchEvent(new Event("resize"))');
  }
  await evaluate('const s=document.querySelector("#diag-speed");s.value="1800";s.dispatchEvent(new Event("change"))');
  await until('document.querySelector("#diag-plot .js-plotly-plot")?.layout?.annotations?.[0]?.text?.includes("1800 CPM")');
  await evaluate('document.querySelector("#diag-tree-tab input[value=measurements]").click()');
  await until('document.querySelectorAll("#diag-measurements .tree-leaf").length > 1');
  await evaluate('document.querySelectorAll("#diag-measurements .tree-leaf")[1].click()');
  await until('document.querySelectorAll("#diag-measurements .tree-leaf")[1].classList.contains("active")');
  await evaluate('document.querySelector("#diag-tree-tab input[value=assets]").click()');
  await until('document.querySelector("#diag-tree-assets").style.display !== "none" && document.querySelector("#diag-measurements").style.display === "none"');
  await evaluate('document.querySelectorAll("#diag-tree .tree-leaf")[1].click()');
  await until('document.querySelectorAll("#diag-tree .tree-leaf")[1].classList.contains("active")');
  await evaluate('document.querySelectorAll("#diag-tree .tree-leaf")[0].click()');
  await evaluate('document.querySelector("#diag-quantity input[value=velocity]").click()');
  await until('document.querySelector("#diag-plot .js-plotly-plot")?.layout?.yaxis?.title?.text?.startsWith("Velocity")');
  await evaluate('document.querySelector("#diag-view input[value=waveform]").click()');
  await until('document.querySelector("#diag-plot .js-plotly-plot")?.layout?.title?.text === "Waveform"');
  await evaluate(`document.querySelector('#diag-axis input[value="2"]').click()`);
  await until('document.querySelector("#diag-trend .js-plotly-plot")?.layout?.shapes?.length === 0');
  await until('document.querySelector("#diag-plot .js-plotly-plot").getBoundingClientRect().height >= 290');
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
