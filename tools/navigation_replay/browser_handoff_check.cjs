// Read-only check of new handoff diagnostics; no screenshots or game control.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http');
function json(url){return new Promise((resolve,reject)=>http.get(url,{headers:{'Accept-Encoding':'identity'}},r=>{
  const chunks=[];r.on('data',c=>chunks.push(c));r.on('error',reject);
  r.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks).toString()));}catch(e){reject(e);}});
}).on('error',reject));}
(async()=>{
  const [batchName,output]=process.argv.slice(2);
  assert.ok(batchName&&output&&!fs.existsSync(output));
  const base='http://127.0.0.1:8766';
  const catalog=await json(base+'/api/catalog?refresh=1');
  const batch=catalog.batches.find(b=>b.directory.endsWith('/'+batchName));assert.ok(batch);
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const reports=[],errors=[];
  try{
    const page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));
    for(const run of batch.runs){
      await page.goto(base+'/?run='+run.id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:180000});
      const data=await json(base+'/api/run/'+run.id);
      assert.ok(data.decisions.every(d=>d.route_handoff?.revision===1));
      const handoff=data.decisions.find(d=>d.route_handoff.active);
      const waiting=data.decisions.find(d=>d.route_handoff.waiting);
      if(handoff)assert.equal(handoff.execution_origin.length,3);
      for(const [d,text] of [[handoff,'路线交接'],[waiting,'临时等待']]){
        if(!d)continue;
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},Math.ceil(d.t*1000)/1000+.001);
        assert.ok((await page.locator('#reason-code').innerText()).includes(text));
      }
      reports.push({case:run.case,id:run.id,url:base+'/?run='+run.id,handoff:!!handoff,waiting:!!waiting});
    }
    assert.deepEqual(errors,[]);
    assert.ok(reports.some(r=>r.handoff));assert.ok(reports.some(r=>r.waiting));
    fs.writeFileSync(output,JSON.stringify({passed:true,reports,page_errors:errors},null,2));
    console.log(JSON.stringify(reports));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
