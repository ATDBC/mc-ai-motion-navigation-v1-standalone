// Read-only browser verification of sealed new/old memory batches; no screenshots.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http');
function json(url){return new Promise((resolve,reject)=>http.get(url,{headers:{'Accept-Encoding':'identity'}},r=>{
  const chunks=[];r.on('data',c=>chunks.push(c));r.on('error',reject);
  r.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks).toString()));}catch(e){reject(e);}});
}).on('error',reject));}
(async()=>{
  const [batchName,output]=process.argv.slice(2);assert.ok(batchName&&output&&!fs.existsSync(output));
  const base='http://127.0.0.1:8766',catalog=await json(base+'/api/catalog?refresh=1');
  const batch=catalog.batches.find(b=>b.directory.endsWith('/'+batchName));assert.ok(batch);
  const browser=await chromium.launch({channel:'msedge',headless:true});const reports=[],errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1100}});page.on('pageerror',e=>errors.push(e.message));
    for(const run of batch.runs){
      assert.ok(run.complete);await page.goto(base+'/?run='+run.id+'&batch='+batch.id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:180000});
      assert.equal(await page.locator('#batch').inputValue(),batch.id);
      assert.equal(await page.locator('#run option').count(),batch.runs.length);
      const data=await json(base+'/api/run/'+run.id);assert.equal(data.parser_version,8);
      if(run.case.endsWith('_low')){
        assert.match(await page.locator('#run option:checked').innerText(),/一格高墙/);
        assert.equal(data.case,run.case);
        assert.deepEqual([...new Set(data.layout.obstacles.map(b=>b.y))],[-60]);
        assert.equal(data.layout.bounds.max_x,21);assert.equal(data.layout.bounds.max_z,21);
      }
      assert.ok(data.memory.available);assert.match(await page.locator('#memory-status').innerText(),/核对通过/);
      if(data.memory.retained_in_radius)assert.match(await page.locator('#memory-status').innerText(),/32 格球内保留旧地形/);
      const initialCells=new Map();
      for(const f of data.knowledge){
        assert.ok(f.upsert.every(c=>typeof c[7]==='number'&&c[7]<=f.t+.000001));
        if(f.t>0)continue;
        for(const key of f.remove)initialCells.delete(key);
        for(const c of f.upsert)initialCells.set(c.slice(0,3).join(','),c);
      }
      const aged=[...initialCells.values()].filter(c=>c[7]<-60);
      if(!data.memory.retained_in_radius)assert.equal(aged.length,0);
      assert.ok(data.decisions.every(d=>d.exploration?.revision===2&&d.observation_attempts?.revision===1));
      for(const t of [data.duration,0,data.duration/2]){
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},t);
        assert.ok(Math.abs(parseFloat(await page.locator('#time-value').innerText())-t)<.002);
      }
      const withAttempt=data.decisions.find(d=>d.observation_attempts.active.length||d.observation_attempts.recent.length);
      if(withAttempt){
        // The slider has millisecond steps; do not round before the first decision.
        const afterDecision=Math.min(data.duration,Math.ceil(withAttempt.t*1000)/1000+.001);
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},afterDecision);
        assert.match(await page.locator('#reason-code').innerText(),/观察 observation\/a\//);
      }
      assert.match(await page.locator('#reason-code').innerText(),/记录 \d+（摘要 \d+）/);
      const movingLook=data.decisions.find(d=>d.observation_opportunity?.mode==='moving');
      const fine=data.decisions.find(d=>d.fine_gaze?.active);
      if(fine){
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},Math.ceil(fine.t*1000)/1000+.001);
        assert.match(await page.locator('#reason-code').innerText(),/精细停步/);
      }
      if(movingLook){
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},Math.ceil(movingLook.t*1000)/1000+.001);
        assert.match(await page.locator('#reason-code').innerText(),/沿路线补看/);
      }
      if(aged.length){
        await page.locator('#height').selectOption('all');
        await page.locator('#seek').evaluate(el=>{el.value='0';el.dispatchEvent(new Event('input'));});
        const rect=await page.locator('#map').boundingBox(),b=data.layout.bounds;
        const width=b.max_x+1-b.min_x,depth=b.max_z+1-b.min_z;
        const scale=Math.min((rect.width-78)/width,(rect.height-62)/depth),old=aged[0];
        await page.mouse.move(rect.x+(rect.width-scale*width)/2+(old[0]+.5-b.min_x)*scale,
          rect.y+(rect.height-scale*depth)/2-3+(b.max_z+1-old[2]-.5)*scale);
        assert.match(await page.locator('#tooltip').innerText(),/历史地形，当前未观测/);
        assert.match(await page.locator('#tooltip').innerText(),/最后观察距今/);
      }
      reports.push({case:run.case,id:run.id,url:base+'/?run='+run.id+'&batch='+batch.id,imports:data.memory.imports_checked,retained:data.memory.retained_in_radius,aged_at_start:aged.length});
    }
    // Legacy links still open the requested run when no batch was specified.
    const first=batch.runs[0];await page.goto(base+'/?run='+first.id);
    await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:180000});
    assert.equal(await page.locator('#run').inputValue(),first.id);
    assert.deepEqual(errors,[]);fs.writeFileSync(output,JSON.stringify({passed:true,reports,page_errors:errors,explicit_batch_and_legacy_links:true},null,2));
    console.log(JSON.stringify(reports));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
