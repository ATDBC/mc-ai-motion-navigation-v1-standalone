// Read-only replay compatibility check using the project's installed browser.
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
      assert.ok(data.memory.available);assert.equal(data.memory.imports_checked,data.memory.imports_expected);
      let checked=0,waitLabels=0;
      if(id===current){
        const decisions=data.decisions.filter(d=>d.execution_continuity?.active&&d.execution_continuity.checked_path?.length>2);
        assert.ok(decisions.length);
        await page.evaluate(()=>{
          window.orangePaths=[];let path=[];
          for(const name of ['beginPath','moveTo','lineTo','stroke']){
            const original=CanvasRenderingContext2D.prototype[name];
            CanvasRenderingContext2D.prototype[name]=function(...args){
              if(name==='beginPath')path=[];
              if(name==='moveTo'||name==='lineTo')path.push(args);
              if(name==='stroke'&&this.strokeStyle==='#df9a4b')window.orangePaths.push(path.slice());
              return original.apply(this,args);
            };
          }
        });
        for(const item of decisions.slice(0,5)){
          assert.equal(item.route_handoff.revision,2);
          assert.deepEqual(item.path,item.execution_continuity.checked_path.map(p=>[p.x,p.y,p.z]));
          await page.evaluate(()=>{window.orangePaths=[];});
          await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},item.t+.001);
          assert.ok((await page.locator('#reason-code').innerText()).includes('沿保留道路'));
          assert.ok(await page.evaluate(n=>window.orangePaths.some(p=>p.length===n),item.path.length));
          checked++;
        }
        for(const item of data.decisions.filter(d=>d.execution_continuity?.temporary_wait).slice(0,3)){
          await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},item.t+.001);
          assert.ok((await page.locator('#reason-code').innerText()).includes('临时等待'));
        }
        const waiting=data.decisions.filter(d=>d.reason==='control_limited_turn'&&d.execution_continuity?.temporary_wait
          &&!d.navigation_observation?.hold&&!d.look?.yaw_delta_degrees&&!d.look?.pitch_delta_degrees);
        for(const item of waiting){
          await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},item.t+.001);
          assert.equal(await page.locator('#reason').innerText(),'等待后续可走路线');
          waitLabels++;
        }
      }else{
        assert.ok(data.decisions.every(d=>!d.execution_continuity));
        await page.locator('#seek').evaluate((el,t)=>{el.value=String(t);el.dispatchEvent(new Event('input'));},data.duration/2);
      }
      reports.push({id,checked_paths:checked,wait_labels:waitLabels,memory_imports:data.memory.imports_checked});
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(output,JSON.stringify({passed:true,reports,page_errors:errors},null,2));
    console.log(JSON.stringify(reports));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
