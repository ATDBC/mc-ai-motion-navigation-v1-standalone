import assert from 'node:assert/strict';
import fs from 'node:fs';
assert.ok(fs.existsSync(new URL('./web/timeline.mjs', import.meta.url)), '时间轴模块尚未实现');
const {KnowledgeTimeline, projectLayers, lastAt, terrainAt,terrainTimeText} = await import('./web/timeline.mjs');
const layout={ground_y:-61,obstacles:[],pit_cells:[{x:6,z:6}]};
const changes=[{t:2,confirmed_t:3,blocks:[{x:7,y:-61,z:8,block:'minecraft:air'}]}];
assert.equal(terrainAt(layout,changes,1).pits.length,1);
assert.equal(terrainAt(layout,changes,2.5).pending.length,1);
assert.equal(terrainAt(layout,changes,2.5).pits.length,1);
assert.equal(terrainAt(layout,changes,3).pits.length,2);
assert.equal(terrainAt(layout,changes,0).pits.length,1,'倒退不得保留未来的坑');
const wall=[{t:2,confirmed_t:3,blocks:[{x:7,y:-60,z:8,block:'minecraft:stone'}]}];
assert.equal(terrainAt(layout,wall,2).walls.length,0);
assert.equal(terrainAt(layout,wall,3).walls.length,1);
assert.equal(terrainAt(layout,wall,3,'-61').walls.length,0);
const a=[1,-61,2,'stone','full_cube',null], b=[2,-61,2,'stone','full_cube',null];
const frames=[
  {t:0,upsert:[a],remove:[],seen:['1,-61,2']},
  {t:1,upsert:[b],remove:[],seen:['2,-61,2']},
  {t:2,upsert:[],remove:['1,-61,2'],seen:[]}
];
const timeline=new KnowledgeTimeline(frames);
frames[1].protected=['1,-61,2'];
assert.equal(timeline.seek(1).cells.size,2);
assert.equal(timeline.protected.has('1,-61,2'),true);
assert.equal(projectLayers(timeline.cells,timeline.seen,'all',-61).get('1,2').color,'memory');
assert.equal(projectLayers(timeline.cells,timeline.seen,'all',-61).get('2,2').color,'observed');
assert.equal(timeline.seek(2).cells.size,1);
assert.equal(timeline.protected.size,0,'未记录保护的后续帧不沿用旧标签');
assert.equal(timeline.seek(0).cells.has('2,-61,2'),false,'倒放不能保留未来记忆');
assert.equal(timeline.cells.size,1);
assert.equal(lastAt(frames,-1),-1);
assert.equal(lastAt(frames,1),1);
assert.equal(projectLayers(new Map([['1,-61,2',a]]),new Set(),-60,-61).size,0);
const tall=[1,-60,2,'stone','full_cube',null];
const mixed=projectLayers(new Map([['1,-61,2',a],['1,-60,2',tall]]),new Set(['1,-61,2']),'all',-61);
assert.equal(mixed.get('1,2').color,'observed','蓝色优先，不叠成紫色');
assert.equal(mixed.get('1,2').records.length,2);
console.log('时间定位、倒放、遗忘、分层、蓝色优先检查通过');
const anonymous=[1,-61,2,'mc2p:navigation_geometry','full_cube',null,['navigation_volume']];
const identity=[1,-61,2,'minecraft:stone','full_cube',null,['surface_depth'],0];
const volumeTimeline=new KnowledgeTimeline([
  {t:0,upsert:[anonymous],remove:[],seen:['1,-61,2'],visual_upsert:[identity],visual_remove:[],visual_seen:['1,-61,2']},
  {t:1,upsert:[anonymous],remove:[],seen:['1,-61,2'],visual_upsert:[],visual_remove:[],visual_seen:[]},
  {t:2,upsert:[],remove:[],seen:[],visual_upsert:[],visual_remove:['1,-61,2'],visual_seen:[]}
]);
assert.equal(volumeTimeline.seek(1).visualCells.get('1,-61,2')[7],0,'通行重获不能刷新身份时间');
assert.equal(volumeTimeline.visualSeen.size,0,'通行已知不等于本帧识别');
assert.equal(volumeTimeline.seek(2).visualCells.size,0,'几何变化可撤销旧身份');
assert.equal(volumeTimeline.seek(0).visualCells.size,1,'倒放恢复身份历史');
assert.equal(volumeTimeline.visualSeen.size,1);
console.log('通行与身份分离、独立时间、倒放检查通过');
const currentCell=[...anonymous,0,{model:'current_history_5s',current:true,history_batch_start:null}];
const historyCell=[...anonymous,12.1,{model:'current_history_5s',current:false,history_batch_start:10}];
assert.equal(terrainTimeText(currentCell,true,70),'通行信息：当前覆盖（latest）','内容最初来源时间不等于当前通行信息年龄');
assert.match(terrainTimeText(currentCell,false,70),/当前帧未确认/);
assert.match(terrainTimeText(historyCell,false,17),/2.0—7.0 秒前（5 秒分批）/);
assert.equal(terrainTimeText(identity,false,17),'证据距今 17.0 秒','视觉身份仍按原精确时间表达');
const bucketTimeline=new KnowledgeTimeline([
  {t:0,upsert:[currentCell],remove:[],seen:['1,-61,2']},
  {t:12.2,upsert:[historyCell],remove:[],seen:[]},
]);
assert.equal(bucketTimeline.seek(13).cells.get('1,-61,2')[8].current,false);
assert.equal(bucketTimeline.seek(1).cells.get('1,-61,2')[8].current,true,'倒放必须撤销未来历史批次');
console.log('当前覆盖、历史分批、旧时间兼容检查通过');
