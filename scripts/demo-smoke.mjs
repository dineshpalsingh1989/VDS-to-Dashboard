// Dependency-free browser check. Uses installed Chrome and Node 24's WebSocket.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../demo/', import.meta.url));
const types = { 'index.html': 'text/html', 'demo.css': 'text/css', 'app.mjs': 'text/javascript', 'data.mjs': 'text/javascript' };
const prefix = '/Wilcoxon-VDS/';
const server = createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const name = pathname === prefix ? 'index.html' : pathname.slice(prefix.length);
  if (!pathname.startsWith(prefix) || !types[name]) { response.writeHead(404).end(); return; }
  try { response.writeHead(200, {'Content-Type': types[name]}); response.end(await readFile(join(root, name))); }
  catch { response.writeHead(500).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}${prefix}`;
const profile = await mkdtemp(join(tmpdir(), 'vds-demo-browser-'));
const browserPath = process.env.DEMO_BROWSER || (process.platform === 'win32' ? 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' : 'google-chrome');
const args = ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-extensions', '--disable-sync', '--disable-dev-shm-usage', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'];
if (process.env.CI) args.unshift('--no-sandbox');
const browser = spawn(browserPath, args, {stdio:'ignore', windowsHide:true});
let launchError;
browser.on('error', error => { launchError = error; });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
let socket;
try {
  let port;
  for (let i=0;i<100;i++) {
    if (launchError) throw launchError;
    try { port = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; }
    catch { await wait(200); }
  }
  assert(port, 'Chrome did not start');
  const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, {once:true}); socket.addEventListener('error', reject, {once:true}); });
  let sequence=0;
  const pending = new Map(), errors=[];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
    if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error' && !message.params.entry.text.includes('favicon.ico')) errors.push(message.params.entry.text);
    if (pending.has(message.id)) {
      const {resolve,reject,timer} = pending.get(message.id); clearTimeout(timer); pending.delete(message.id);
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    }
  });
  function call(method, params={}) {
    return new Promise((resolve,reject) => {
      const id=++sequence;
      const timer=setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); },10000);
      pending.set(id,{resolve,reject,timer}); socket.send(JSON.stringify({id,method,params}));
    });
  }
  async function evaluate(expression) {
    const result = await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  await call('Page.enable'); await call('Runtime.enable'); await call('Log.enable');
  await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url});
  let ready=false;
  for(let i=0;i<60;i++) { if(await evaluate('document.querySelectorAll(".channel-card").length === 4')) {ready=true;break;} await wait(100); }
  assert(ready,'Four channel cards must render');
  assert.equal(await evaluate('document.querySelector("#asset-condition").textContent'),'Normal');
  assert.equal(await evaluate('document.querySelector("#pause").textContent'),'Resume replay');
  await evaluate('document.querySelector("#scenario").value="rising";document.querySelector("#scenario").dispatchEvent(new Event("change"))');
  assert.equal(await evaluate('document.querySelector("#asset-condition").textContent'),'Warning');
  assert.equal(await evaluate('document.querySelectorAll(".channel-card .warning").length'),1);
  if(process.env.DEMO_SCREENSHOT) {
    const shot=await call('Page.captureScreenshot',{format:'png'});
    await writeFile(process.env.DEMO_SCREENSHOT,Buffer.from(shot.data,'base64'));
  }
  await evaluate('document.querySelectorAll(".channel-card")[1].click()');
  assert((await evaluate('document.querySelector("#trend-title").textContent')).startsWith('CH2'));
  await evaluate('document.querySelector("#window").value="15";document.querySelector("#window").dispatchEvent(new Event("change"))');
  assert((await evaluate('document.querySelector("#trend-chart svg").outerHTML')).includes('12:45'));
  await evaluate('location.hash="diagnostics"'); await wait(100);
  assert.equal(await evaluate('document.querySelector("[data-panel=diagnostics]").hidden'),false);
  await evaluate('document.querySelector("#orders").click()');
  assert((await evaluate('document.querySelector("#spectrum-chart").textContent')).includes('Order'));
  const withBaseline=await evaluate('document.querySelectorAll("#spectrum-chart path").length');
  await evaluate('document.querySelector("#baseline").click()');
  assert.equal(await evaluate('document.querySelectorAll("#spectrum-chart path").length'),withBaseline-1);
  await evaluate('document.querySelector("#scenario").value="missing";document.querySelector("#scenario").dispatchEvent(new Event("change"));document.querySelector("#diag-channel").value="3";document.querySelector("#diag-channel").dispatchEvent(new Event("change"))');
  assert.equal(await evaluate('document.querySelector("#asset-condition").textContent'),'Data incomplete');
  assert.equal(await evaluate('document.querySelector("#reporting").textContent.trim()'),'3 / 4');
  assert((await evaluate('document.querySelector("#spectrum-chart svg").getAttribute("aria-label")')).includes('last known'));
  await evaluate('document.querySelector("#pause").click()');
  await wait(3200);
  await evaluate('document.querySelector("#pause").click()');
  assert(await evaluate('Number(document.querySelectorAll(".channel-card")[2].textContent.match(/Last: ([0-9]+)/)[1]) >= 6'));
  await evaluate('location.hash="alarms"'); await wait(100);
  assert((await evaluate('document.querySelector("#event-rows").textContent')).includes('Stale'));
  await call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:profile});
  await evaluate('location.hash="overview"'); await wait(100);
  await evaluate('document.querySelector("#export").click()');
  let download;
  for(let i=0;i<40;i++) { download=(await readdir(profile)).find(file => file.endsWith('.csv'));if(download)break;await wait(100); }
  assert(download,'CSV download must complete');
  assert((await readFile(join(profile,download),'utf8')).includes('synthetic_demo'));
  await evaluate('document.querySelector("#reset").click()');
  assert.equal(await evaluate('document.querySelector("#asset-condition").textContent'),'Normal');
  await evaluate('document.querySelector("#pause").click()');
  assert.equal(await evaluate('document.querySelector("#pause").textContent'),'Pause replay');
  await evaluate('document.querySelector("#pause").click()');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  for(const view of ['overview','diagnostics','alarms','guide']) {
    await evaluate(`location.hash=${JSON.stringify(view)}`); await wait(100);
    assert(await evaluate('document.documentElement.scrollWidth <= 390'),`No horizontal page overflow in ${view}`);
  }
  await evaluate('location.hash="toString"'); await wait(100);
  assert.equal(await evaluate('document.querySelector("#page-title").textContent'),'Pump overview');
  assert.deepEqual(errors,[], 'No browser runtime or CSP errors');
  console.log('Browser passed: scenarios, channels, history, spectrum, routes, CSV, replay controls, and four mobile views.');
  await call('Browser.close');
} finally {
  socket?.close(); browser.kill();
  await new Promise(resolve => server.close(resolve));
  await rm(profile,{recursive:true,force:true,maxRetries:8,retryDelay:200});
}
