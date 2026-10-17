// Reuse the project's bundled Playwright. No install, visible window or screenshot.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs');
function json(url){return new Promise((resolve,reject)=>http.get(url,{headers:{'Accept-Encoding':'identity'}},r=>{const chunks=[];r.on('data',c=>chunks.push(c));r.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks)))}catch(e){reject(e)}});r.on('error',reject)}).on('error',reject));}
(async()=>{
  const base='http://127.0.0.1:8766',id=process.argv[2],out=process.argv[3];
  assert.ok(id&&out,'provide run id and a new output file');assert.ok(!fs.existsSync(out));
  const data=await json(base+'/api/run/'+id);
  assert.equal(data.navigation_volume,true);assert.equal(data.memory.error,null);
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(base+'/?run='+id);await page.waitForFunction(()=>!document.getElementById('play').disabled,{},{timeout:180000});
    assert.match(await page.locator('#map-note').innerText(),/通行几何/);
    assert.match(await page.locator('#memory-status').innerText(),/核对通过/);
    if(data.current_history_batches){
      assert.match(await page.locator('#map-note').innerText(),/latest/);
      assert.ok(data.knowledge.some(f=>f.upsert.some(c=>c[8]?.model==='current_history_5s')));
    }
    for(const t of [data.duration/2,data.duration,0]){
      await page.locator('#seek').evaluate((e,t)=>{e.value=t;e.dispatchEvent(new Event('input'))},t);
      assert.ok(Math.abs(parseFloat(await page.locator('#time-value').innerText())-t)<.002);
    }
    const bounds=data.layout.bounds;
    const visibleCell=c=>c[1]===data.layout.ground_y&&(c[6]??[]).includes('navigation_volume')&&
      c[0]>=bounds.min_x&&c[0]<=bounds.max_x&&c[2]>=bounds.min_z&&c[2]<=bounds.max_z;
    const frame=data.knowledge.find(f=>f.t>=0&&f.upsert.some(visibleCell));
    assert.ok(frame);
    const cell=frame.upsert.find(visibleCell);
    // The range input rounds to milliseconds. Never seek just before the
    // selected knowledge frame when its actual timestamp has finer precision.
    const frameTime=Math.ceil(frame.t*1000)/1000;
    await page.locator('#seek').evaluate((e,t)=>{e.value=t;e.dispatchEvent(new Event('input'))},frameTime);
    await page.locator('#height').selectOption(String(data.layout.ground_y));
    await page.locator('#map').scrollIntoViewIfNeeded();
    const r=await page.locator('#map').boundingBox(),b=data.layout.bounds,nx=b.max_x+1-b.min_x,nz=b.max_z+1-b.min_z;
    const scale=Math.min((r.width-78)/nx,(r.height-62)/nz),left=(r.width-scale*nx)/2,top=(r.height-scale*nz)/2-3;
    await page.locator('#map').hover({position:{x:left+(cell[0]+.5-b.min_x)*scale,y:top+(b.max_z+.5-cell[2])*scale}});
    const tooltip=await page.locator('#tooltip').innerText();
    assert.match(tooltip,/通行信息/);assert.match(tooltip,/身份/);assert.deepEqual(errors,[]);
    if(data.current_history_batches&&cell[8]?.current){assert.match(tooltip,/latest/);}
    fs.writeFileSync(out,JSON.stringify({passed:true,run:id,source:data.source,memory:data.memory,
      current_history_batches:!!data.current_history_batches,tooltip,page_errors:errors},null,2));
    console.log(JSON.stringify({passed:true,run:id}));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
