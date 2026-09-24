const {chromium}=require('C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');

(async()=>{
  const base=process.argv[2]??'http://127.0.0.1:8766';
  const catalog=await(await fetch(base+'/api/catalog?refresh=1')).json();
  const batch=catalog.batches.find(item=>item.experiment==='B03 运动形状');
  assert.ok(batch,'未发现 B03 运动形状证据');
  assert.equal(batch.runs.length,7);
  const line=batch.runs.find(item=>item.case==='shape_line_rotating_view');
  assert.ok(line);
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`${base}/?run=${line.id}&batch=${batch.id}`);
    await page.waitForFunction(()=>!document.getElementById('play').disabled,null,{timeout:30000});
    assert.equal(await page.locator('#scene-title').innerText(),'平视直行 · 顺逆时针各转一圈');
    assert.ok(await page.locator('#shape-card').isVisible());
    assert.match(await page.locator('#shape-metrics').innerText(),/横向 RMS/);
    await page.locator('#seek').evaluate((element)=>{
      element.value=String(Number(element.max)/2);element.dispatchEvent(new Event('input'));
    });
    assert.match(await page.locator('#shape-error-now').innerText(),/横向误差/);
    const chart=await page.locator('#error-chart').evaluate(element=>({width:element.width,height:element.height}));
    assert.ok(chart.width>0&&chart.height>0);
    const before=await page.locator('#elapsed').innerText();
    await page.locator('#play').click();await page.waitForTimeout(250);await page.locator('#play').click();
    assert.notEqual(await page.locator('#elapsed').innerText(),before,'播放时回放时间应前进');
    fs.mkdirSync('output/playwright/navigation-replay',{recursive:true});
    await page.screenshot({path:'output/playwright/navigation-replay/b03-shape-line.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,batch:batch.id,run:line.id,chart}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
