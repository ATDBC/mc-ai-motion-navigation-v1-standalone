// Reuse the project's installed browser library; read-only, without images.
const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
  const [current,legacy,output]=process.argv.slice(2);
  assert.ok(current&&legacy&&output&&!fs.existsSync(output));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const reports=[],errors=[];
  try{
    const page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));
    for(const id of [current,legacy]){
      await page.goto('http://127.0.0.1:8766/?run='+id);
      await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:180000});
      const data=await page.evaluate(async id=>(await fetch('/api/run/'+id)).json(),id);
      assert.equal(data.parser_version,10);
      const item=data.decisions.find(d=>d.navigation_purpose?.observation_travel);
      if(id===current){
        assert.ok(item);assert.equal(item.observation_travel_revision,1);
        await page.evaluate(()=>{
          window.drawnObservationLabels=[];
          const original=CanvasRenderingContext2D.prototype.fillText;
          CanvasRenderingContext2D.prototype.fillText=function(text,...args){
            window.drawnObservationLabels.push(String(text));return original.call(this,text,...args);
          };
        });
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},item.t+.002);
        assert.ok(await page.evaluate(()=>window.drawnObservationLabels.some(t=>t.startsWith('观察参考'))));
        assert.ok((await page.locator('#reason-code').innerText()).includes('观察'));
        if(item.exploration_integration){
          await page.locator('#observation-details summary').click();
          assert.ok((await page.locator('#observation-detail-text').innerText()).includes('参考位置'));
        }
      }else{
        assert.ok(data.decisions.every(d=>!d.exploration_integration));
        await page.locator('#observation-details summary').click();
        assert.ok((await page.locator('#observation-detail-text').innerText()).includes('旧记录未提供'));
      }
      reports.push({id,parser_version:data.parser_version,has_separate_observation:!!item,
        memory_imports:data.memory.imports_checked});
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(output,JSON.stringify({passed:true,reports,page_errors:errors},null,2));
    console.log(JSON.stringify(reports));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
