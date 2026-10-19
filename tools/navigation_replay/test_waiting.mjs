import assert from 'node:assert/strict';
import fs from 'node:fs';

const source=fs.readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const match=source.match(/function waitingText\(waiting\)\{[\s\S]*?\n\}/);
assert.ok(match,'waitingText helper missing');
const waitingText=Function(`${match[0]};return waitingText;`)();

assert.equal(waitingText(undefined),null,'old replay must not invent a reason');
assert.equal(waitingText({revision:1,category:'none'}),null,'moving frames are not waiting');
assert.equal(waitingText({revision:1,category:'computation'}),'等待路线计算');
assert.equal(waitingText({revision:1,category:'evidence'}),'等待合法观察证据');
assert.equal(waitingText({revision:1,category:'connector_refused'}),'当前身体暂时接不上路线');
assert.equal(waitingText({revision:1,category:'braking'}),'正在制动，等待后继路线');
assert.match(source,/临时候选（尚未采用）/);
assert.match(source,/正式检查点/);
assert.match(source,/候选已满足采用条件/);
const scheduleMatch=source.match(/function searchScheduleText\(schedule\)\{[\s\S]*?\n\}/);
assert.ok(scheduleMatch,'searchScheduleText helper missing');
const scheduleText=Function(`${scheduleMatch[0]};return searchScheduleText;`)();
assert.equal(scheduleText(undefined),'');
assert.equal(scheduleText({mode:'none'}),'');
assert.match(scheduleText({mode:'required_route'}),/上次后台搜索：停稳接路/);
const idleText=scheduleText({mode:'none',previous_idle:{mode:'required_route',budget_ns:8000000,elapsed_ns:8200000}});
assert.match(idleText,/最近一次空闲搜索：停稳接路/);
assert.match(idleText,/额度 8\.00 ms，实际 8\.20 ms/);
assert.doesNotMatch(idleText,/本轮控制前/);
const deadlineMatch=source.match(/function controlDeadlineText\(deadline\)\{[\s\S]*?\n\}/);
assert.ok(deadlineMatch,'controlDeadlineText helper missing');
const controlDeadlineText=Function(`${deadlineMatch[0]};return controlDeadlineText;`)();
assert.equal(controlDeadlineText(undefined),'');
assert.match(controlDeadlineText({revision:1,status:'background_work_pending',
  withdraw_input:false,elapsed_ns:400000,deadline_ns:1000000,
  pending_reason:'global_work_over_budget'}),/后台规划继续.*没有释放输入/);
assert.match(controlDeadlineText({revision:1,status:'local_control_deadline',
  withdraw_input:true,elapsed_ns:1200000,deadline_ns:1000000,
  pending_reason:null}),/局部控制超过截止时间.*释放输入/);
assert.match(source,/当前走廊/);
assert.match(source,/局部轨迹/);
assert.match(source,/地图变化/);
console.log('等待分类与新旧回放兼容检查通过');
