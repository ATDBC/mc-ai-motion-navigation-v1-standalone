// Check a specific sealed maze batch without opening windows or taking images.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
function readJson(url){return new Promise((resolve,reject)=>{
  http.get(url,{headers:{'Accept-Encoding':'identity'}},response=>{
    const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('error',reject);
    response.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch(e){reject(e);}});
  }).on('error',reject);
});}
(async()=>{
  const [batchName,output,expectedRevision='1']=process.argv.slice(2);
  assert.ok(batchName&&output,'Pass the matrix directory name and a fresh output file');
  assert.ok(!fs.existsSync(output),'Keep previous evidence');
  const base='http://127.0.0.1:8766';
  const catalog=await readJson(base+'/api/catalog?refresh=1');
  const batch=catalog.batches.find(b=>b.directory.endsWith('/'+batchName));
  assert.ok(batch);assert.equal(batch.runs.length,3);
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[],reports=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    for(const run of batch.runs){
      assert.ok(run.complete);
      await page.goto(base+'/?run='+run.id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,{},{timeout:180000});
      const data=await readJson(base+'/api/run/'+run.id);
      assert.equal(data.layout.bounds.max_x,run.case==='maze_01'?15:21);
      assert.match(await page.locator('#scene-title').innerText(),/迷宫/);
      assert.match(await page.locator('#memory-status').innerText(),/核对通过/);
      assert.ok(data.decisions.some(d=>d.exploration?.revision===Number(expectedRevision)));
      if(expectedRevision==='2')assert.ok(data.decisions.some(d=>d.observation_attempts?.revision===1));
      for(const t of [0,data.duration/2,data.duration]){
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},t);
        assert.ok(Math.abs(parseFloat(await page.locator('#time-value').innerText())-t)<.002);
      }
      assert.equal(await page.locator('#position').innerText(),data.samples.at(-1).position.map(v=>v.toFixed(2)).join(' / '));
      reports.push({case:run.case,id:run.id,duration:data.duration,url:base+'/?run='+run.id});
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(output,JSON.stringify({passed:true,batch:batch.id,reports,page_errors:errors},null,2));
    console.log(JSON.stringify(reports));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
