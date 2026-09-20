const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');

(async()=>{
  const base=process.argv[2]??'http://127.0.0.1:8766';
  const catalog=await(await fetch(base+'/api/physics/catalog?refresh=1')).json();
  assert.ok(catalog.runs.some(run=>run.mode==='b08-ground'&&run.complete));
  assert.ok(catalog.runs.some(run=>run.mode==='b08-crawl'&&run.complete));
  assert.ok(catalog.runs.some(run=>run.mode==='b09-air'&&run.complete));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/physics.html');
    await page.waitForFunction(()=>document.querySelectorAll('#sequence option').length>1,null,{timeout:30000});
    assert.match(await page.locator('#status').innerText(),/已按 minecraft-java-1_21-player-motion-r1 计算/);
    assert.ok(Number((await page.locator('#tick-count').innerText()).replaceAll(',',''))>0);
    for(const id of ['top-view','side-view','error-view']){
      const size=await page.locator('#'+id).evaluate(element=>({width:element.width,height:element.height}));
      assert.ok(size.width>100&&size.height>100,`${id} 未绘制`);
    }
    const before=await page.locator('#frame').innerText();
    await page.locator('#play').click();await page.waitForTimeout(220);await page.locator('#play').click();
    assert.notEqual(await page.locator('#frame').innerText(),before,'播放时 tick 应前进');
    await page.locator('#sequence').selectOption({index:1});
    assert.match(await page.locator('#segment-reason').innerText(),/切段原因/);
    const modes=await page.locator('#run option').allTextContents();
    const airIndex=modes.findIndex(value=>value.includes('B09 空中转换'));
    assert.ok(airIndex>=0);
    await page.locator('#run').selectOption({index:airIndex});
    await page.waitForFunction(()=>document.getElementById('tick-count').textContent.replaceAll(',','')==='1948',null,{timeout:30000});
    fs.mkdirSync('output/playwright/navigation-replay',{recursive:true});
    await page.screenshot({path:'output/playwright/navigation-replay/b09r-physics-calculator.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,runs:catalog.runs.filter(r=>r.complete).length,screenshot:'output/playwright/navigation-replay/b09r-physics-calculator.png'}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
