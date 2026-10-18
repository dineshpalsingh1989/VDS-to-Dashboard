// Shared Chrome DevTools harness; no browser automation dependencies required.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
export async function openBrowser(width = 1640, height = 1100) {
  const profile = await mkdtemp(join(tmpdir(), 'vds-demo-browser-'));
  const executable = process.env.DEMO_BROWSER || (process.platform === 'win32'
    ? 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' : 'google-chrome');
  const args = ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-networking','--disable-extensions','--disable-sync','--disable-dev-shm-usage',
    '--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'];
  if (process.env.CI) args.unshift('--no-sandbox');
  const processHandle = spawn(executable, args, {stdio:'ignore', windowsHide:true});
  let launchError, socket;
  processHandle.on('error', error => { launchError = error; });
  async function cleanup() {
    socket?.close();
    processHandle.kill();
    await rm(profile, {recursive:true, force:true, maxRetries:10, retryDelay:200});
  }
  try {
    let port;
    for (let i=0;i<150;i++) {
      if (launchError) throw launchError;
      try { port = (await readFile(join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]; break; }
      catch { await wait(200); }
    }
    assert(port,'Chrome did not start');
    const pages=await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    socket=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    let sequence=0;
    const pending=new Map(), errors=[];
    socket.addEventListener('message',event=>{
      const message=JSON.parse(event.data);
      if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
      if(message.method==='Log.entryAdded' && message.params.entry.level==='error' && !message.params.entry.url?.endsWith('favicon.ico')) errors.push(message.params.entry.text);
      if(pending.has(message.id)) {
        const {resolve,reject,timer}=pending.get(message.id);clearTimeout(timer);pending.delete(message.id);
        message.error?reject(new Error(JSON.stringify(message.error))):resolve(message.result);
      }
    });
    function call(method,params={}) {
      return new Promise((resolve,reject)=>{
        const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(new Error(`Timed out: ${method}`));},30000);
        pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));
      });
    }
    async function evaluate(expression) {
      const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    }
    async function until(expression) {
      for(let i=0;i<150;i++) {if(await evaluate(expression))return;await wait(100);}
      throw new Error(`Page not ready: ${expression}`);
    }
    await call('Page.enable');await call('Runtime.enable');await call('Log.enable');
    await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
    return {call,evaluate,until,errors,profile,async close(){try{await call('Browser.close');}finally{await cleanup();}}};
  } catch(error) {await cleanup();throw error;}
}
