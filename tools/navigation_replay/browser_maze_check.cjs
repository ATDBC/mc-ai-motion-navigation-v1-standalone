// Exercise the repository's existing bundled Playwright setup without installing packages.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
// Avoid the bundled Node fetch parser's failure on large compressed replay responses.
function readJson(url){return new Promise((resolve,reject)=>{
  http.get(url,{headers:{'Accept-Encoding':'identity'}},response=>{
    const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('error',reject);
    response.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch(error){reject(error);}});
  }).on('error',reject);
});}

(async()=>{
  const base=process.argv[2]||'http://127.0.0.1:8766';
  const catalog=await readJson(base+'/api/catalog?refresh=1');
  const batch=catalog.batches.find(b=>['maze_01','maze_02','maze_03'].every(c=>b.runs.some(r=>r.case===c&&r.complete)));
  assert.ok(batch,'缺少三个完整迷宫记录');
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  await page.addInitScript(()=>{
    const proto=CanvasRenderingContext2D.prototype,clear=proto.clearRect,fill=proto.fillText;
    proto.clearRect=function(...args){window.mazeStageLabels=[];return clear.apply(this,args);};
    proto.fillText=function(text,...args){if(this.fillStyle==='#a87835')window.mazeStageLabels.push(String(text));return fill.call(this,text,...args);};
  });
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const output=path.resolve('output/navigation-mazes-v1/browser');fs.mkdirSync(output,{recursive:true});
  const reports=[];
  try{
    for(const run of batch.runs){
      await page.goto(base+'/?run='+run.id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,{},{timeout:180000});
      const data=await readJson(base+'/api/run/'+run.id);
      assert.equal(data.layout.bounds.max_x,run.case==='maze_01'?15:21);
      assert.equal(data.layout.bounds.max_z,data.layout.bounds.max_x);
      assert.match(await page.locator('#scene-title').innerText(),/迷宫/);
      assert.match(await page.locator('#memory-status').innerText(),/核对通过/);
      assert.equal(Number(await page.locator('#seek').getAttribute('max')),data.duration);
      for(const t of [data.duration/2,data.duration,0]){
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},t);
        assert.ok(Math.abs(parseFloat(await page.locator('#time-value').innerText())-t)<.002);
      }
      const rect=await page.locator('#map').boundingBox(),size=data.layout.bounds.max_x+1;
      const scale=Math.min((rect.width-78)/size,(rect.height-62)/size);
      const left=(rect.width-scale*size)/2,top=(rect.height-scale*size)/2-3;
      await page.mouse.move(rect.x+left+(size-.5)*scale,rect.y+top+.5*scale);
      assert.match(await page.locator('#tooltip').innerText(),new RegExp(`格子 \\(${size-1}, ${size-1}\\)`));
      await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},data.duration);
      const expected=data.samples.at(-1).position.map(v=>v.toFixed(2)).join(' / ');
      assert.equal(await page.locator('#position').innerText(),expected);
      const uniqueTargets=new Set(data.decisions.filter(d=>d.target).map(d=>JSON.stringify(d.target))).size;
      assert.equal(await page.evaluate(()=>window.mazeStageLabels.length),uniqueTargets,'同一位置只绘一个阶段编号');
      assert.ok(await page.locator('#events button').count()>=uniqueTargets,'完整目标列表仍可查看');
      await page.mouse.move(10,10);
      await page.screenshot({path:path.join(output,run.case+'.png'),fullPage:true});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.setViewportSize({width:1440,height:1100});
      reports.push({case:run.case,id:run.id,duration:data.duration,body_samples:data.samples.length,
        bounds:data.layout.bounds,memory:data.memory,final_position:data.samples.at(-1).position});
    }
    await page.locator('#restart').click();await page.locator('#speed').selectOption('8');
    await page.locator('#play').click();await page.waitForTimeout(500);await page.locator('#play').click();
    assert.ok(parseFloat(await page.locator('#time-value').innerText())>1);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'checks.json'),JSON.stringify({passed:true,batch:batch.id,reports,page_errors:errors},null,2));
    console.log(JSON.stringify({passed:true,batch:batch.id,runs:reports.map(r=>({case:r.case,id:r.id,duration:r.duration}))}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
