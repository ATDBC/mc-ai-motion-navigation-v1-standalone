const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
  const base='http://127.0.0.1:8766';
  const catalog=await(await fetch(base+'/api/catalog?refresh=1')).json();
  const batch=catalog.batches.find(b=>b.directory.endsWith('matrix-20260912T013028950942Z-3ae39e34'));
  assert.equal(batch.runs.length,4);
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const results=[];
    for(const run of batch.runs){
      await page.goto(base+'/?run='+run.id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:180000});
      const data=await(await page.request.get(base+'/api/run/'+run.id)).json();
      assert.ok(data.memory.available);
      if(run.case==='pit')assert.equal(data.layout.pit_cells.length,9);
      else{
        const event=data.changes[0];assert.ok(event.confirmed_t>event.t);
        await page.locator('#seek').evaluate((el,t)=>{el.value=t;el.dispatchEvent(new Event('input'));},(event.t+event.confirmed_t)/2);
        assert.match(await page.locator('#change-status').innerText(),/尚无合法观察确认/);
        await page.locator('#change-events button').last().click();
        assert.match(await page.locator('#change-status').innerText(),/已获合法确认/);
        if(run.case==='terrain_entity'){
          const seen=data.entities.filter(f=>f.items.some(e=>e.id===event.entity_id));assert.ok(seen.length>1);
          assert.notDeepEqual(seen[0].items[0].position,seen.at(-1).items[0].position);
          const gone=data.entities.find(f=>f.t>seen.at(-1).t&&!f.items.length);assert.ok(gone,'实体离开视野后应有空帧');
        }
      }
      fs.mkdirSync('output/playwright/navigation-replay',{recursive:true});
      await page.screenshot({path:`output/playwright/navigation-replay/${run.case}.png`,fullPage:true});
      if(data.changes.length){await page.locator('#restart').click();assert.match(await page.locator('#change-status').innerText(),/尚未提交/);}
      results.push({case:run.case,id:run.id,imports:data.memory.imports_checked,change:data.changes[0]});
    }
    assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,results}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
