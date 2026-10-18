import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const root=new URL('../',import.meta.url);
const demo=JSON.parse(await readFile(new URL('demo/snapshots.json',root),'utf8'));

test('all 14 application routes and the actual navigation are exported',()=>{
  assert.equal(Object.keys(demo.pages).length,14);
  for(const path of Object.keys(demo.pages)) assert(demo.shell.sidebar.includes(`href="${path}"`));
  assert(demo.shell.header.includes('VDS Condition Monitor'));
  for(const text of ['CH1','CH2','CH3','CH4','RPM','Real-time Trends','Sensor type:','Sensitivity:']) assert(demo.pages['/'].html.includes(text));
});
test('demo layout and styles match the current application sources',async()=>{
  for(const [path,hash] of Object.entries(demo.layoutSources)) {
    const content=(await readFile(new URL(path,root),'utf8')).replaceAll('\r\n','\n');
    assert.equal(createHash('sha256').update(content).digest('hex'),hash,`Regenerate demo after changing ${path}`);
  }
  assert.equal((await readFile(new URL('demo/vendor/dashboard.css',root),'utf8')).replaceAll('\r\n','\n'),
    (await readFile(new URL('assets/dashboard.css',root),'utf8')).replaceAll('\r\n','\n'));
});
test('sample model contains measurement metadata and real plot outputs for each channel',()=>{
  assert.equal(Object.keys(demo.bank.channels).length,4);
  for(const [ch,data] of Object.entries(demo.bank.channels)) {
    assert.equal(data.history.Velocity.length,data.times.length);
    assert.equal(data.metadata.Velocity.unit,'mm/s');
    for(const key of [`diagnostics:${ch}:0`,`diagnostics:${ch}:1`,`waterfall:${ch}`,`waveform:${ch}`,`history:${ch}:Velocity`]) {
      assert(demo.bank.figures[key].data.length>0,key);
    }
  }
});
test('export contains synthetic identities and no live scripts or loading markers',()=>{
  assert.equal(demo.bank.gateway,'VDS-DEMO-01');
  for(const page of Object.values(demo.pages)) {
    assert(!page.html.includes('<script'));
    assert(!page.html.includes('data-dash-is-loading'));
    assert(!page.html.includes('C:\\Users\\'));
  }
});
