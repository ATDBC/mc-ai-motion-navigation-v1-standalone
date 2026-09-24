import {KnowledgeTimeline,lastAt,projectLayers,terrainAt,terrainTimeText} from './timeline.mjs';
const $=id=>document.getElementById(id);
const canvas=$('map'),ctx=canvas.getContext('2d'),errorCanvas=$('error-chart'),errorCtx=errorCanvas.getContext('2d');
const cases={empty:'空地',staggered_walls:'交错墙',side_information:'侧面信息',corner_prelook:'转角',narrow_clearance:'窄口',reposition_view:'移动寻找观察位置',irrelevant_side:'无关侧面信息'};
const outcomes={success:'成功',timeout:'超时',blocked:'受阻',failed:'失败',boundary:'达到能力边界'};
Object.assign(cases,{pit:'坑边绕行',terrain_removed:'途中挖地',terrain_wall_added:'新增墙体',terrain_entity:'动态村民'});
Object.assign(cases,{maze_01:'迷宫 1 · 基础岔路',maze_02:'迷宫 2 · 深支路',maze_03:'迷宫 3 · 环路与岔路'});
Object.assign(cases,{maze_01_pits:'迷宫 1 · 全坑变体'});
Object.assign(cases,{maze_03_pits:'迷宫 3 · 全坑变体'});
Object.assign(cases,{maze_04:'迷宫 4 · 诱导支路与折返',maze_05:'迷宫 5 · 环路汇合与记忆',maze_06:'迷宫 6 · 连续转角与补看'});
for(let i=3;i<=6;i++){const id=`maze_${String(i).padStart(2,'0')}`;cases[`${id}_floating`]=`${cases[id]} · 悬浮墙（第二层）`;}
Object.assign(cases,{maze_07:'迷宫 7 · 连续转角与坑边',maze_08:'迷宫 8 · 墙坑夹道与转弯',maze_09:'迷宫 9 · 断路判断与绕行'});
for (const n of ['03','04','05','06']) cases[`maze_${n}_low`]=`${cases[`maze_${n}`]} · 一格高墙`;
const experiments={'navigation-control-v1':'连续控制优化','j5-static-v1':'冻结策略基线','j5-radius-config':'取消半径限制'};
experiments['navigation-column-air-v1']='竖直列空气推断';
experiments['navigation-volume-v1']='通行几何与视觉身份分离';
experiments['navigation-hierarchical-v1']='分层增量导航';
const reasons={control_limited_turn:'调整朝向或减速',joint_bounded_step:'沿当前路线移动',planning_budget_exhausted:'本轮计算预算用完，释放输入',point_goal_region:'已进入目标范围',joint_observe:'补充观察',bounded_recovery_wait:'等待恢复条件',no_admissible_joint_candidate:'没有可执行的候选动作'};
Object.assign(reasons,{tracking_fixed_route:'沿固定路线行走',corner_speed_control:'接近拐角，控制速度',goal_braking:'接近终点，松开输入制动',goal_reached_and_stopped:'到达终点并停稳'});
reasons.missing_surface_identity='需要确认近处地面的材质';
reasons.surface_identity_unobservable='当前位置暂时无法确认近处地面的材质';
const explorationReasons={no_gain:'这个位置已尝试，尚未得到答案',related_evidence:'相关地形有新证据，重新检查观察机会',time_limit:'本次观察时间已用完',angle_limit:'本次观察转角已用完',attempt_limit:'本次观察次数已用完',answered:'问题已有答案',interrupted:'保留已完成部分，等待接续'};
reasons.search_incomplete='搜索尚未完成，等待后续计算';
function waitingText(waiting){
  if(!waiting||waiting.revision!==1)return null;
  if(waiting.category==='none')return null;
  const labels={none:null,computation:'等待路线计算',evidence:'等待合法观察证据',connector_refused:'当前身体暂时接不上路线',observation:'正在完成必要观察',braking:'正在制动，等待后继路线',dynamic_occupancy:'等待动态占据解除',goal_stop:'已到终点并保持停稳',budget:'本轮预算已用完',control:'等待当前控制完成',safety:'安全控制正在接管'};
  return labels[waiting.category]??`等待原因：${waiting.category??'未记录'}`;
}
function searchScheduleText(schedule){
  if(!schedule)return '';
  const parts=[];
  if(schedule.mode==='required_foreground')parts.push('本轮控制前推进必要路线搜索');
  const previous=schedule.previous_idle;
  if(previous){
    const labels={ordinary:'普通搜索',urgent_route:'临近道路末端或待决岔口',required_route:'停稳接路'};
    parts.push(`最近一次空闲搜索：${labels[previous.mode]??previous.mode}（额度 ${(previous.budget_ns/1e6).toFixed(2)} ms，实际 ${(previous.elapsed_ns/1e6).toFixed(2)} ms）`);
  }else if(schedule.mode==='required_route')parts.push('上次后台搜索：停稳接路');
  else if(schedule.mode==='urgent_route')parts.push('上次后台搜索：临近道路末端或待决岔口（仍为 2 ms）');
  return parts.join(' · ');
}
function controlDeadlineText(deadline){
  if(!deadline||deadline.revision!==1)return '';
  const elapsed=(deadline.elapsed_ns/1e6).toFixed(2),limit=(deadline.deadline_ns/1e6).toFixed(2);
  if(deadline.status==='background_work_pending'&&!deadline.withdraw_input)
    return `后台规划继续（局部复核 ${elapsed} / ${limit} ms），当前动作没有释放输入`;
  if(deadline.status==='local_control_deadline'&&deadline.withdraw_input)
    return `局部控制超过截止时间（${elapsed} / ${limit} ms），已释放输入`;
  if(deadline.status==='ready')return `局部控制按时完成（${elapsed} / ${limit} ms）`;
  return `控制截止状态：${deadline.status??'未记录'}`;
}
function decisionReason(d){
  if(d?.scanning)return '开局转头扫描';
  if(d?.navigation_control_deadline?.status==='local_control_deadline')return '局部控制超时，安全释放输入';
  if(d?.navigation_control_deadline?.status==='background_work_pending'&&d?.reason==='planning_budget_exhausted')return '沿当前安全路线移动，后台继续规划';
  const recorded=waitingText(d?.waiting);if(recorded)return recorded;
  const moving=!!(d?.movement?.forward||d?.movement?.strafe);
  const turning=!!(d?.look?.yaw_delta_degrees||d?.look?.pitch_delta_degrees);
  const waiting=d?.execution_continuity?.temporary_wait||d?.route_handoff?.waiting;
  if(d?.reason==='control_limited_turn'){
    if(d.navigation_observation?.hold)return '停留补充观察';
    if(turning)return '原地调整视角';
    if(waiting||d.navigation_purpose?.route_status==='pending')return '等待后续可走路线';
    return '保持零移动输入';
  }
  if(waiting&&moving&&d?.reason==='joint_bounded_step')return '等待接路，调整移动';
  return reasons[d?.reason]??(d?.reason?'查看下方原始原因':'尚未开始决策');
}
let catalog=[],data=null,knowledge=null,time=0,playing=false,lastAnimation=null,generation=0,currentRun=null;
let columns=new Map(),transform=null,eventKey=null,stageEvents=[];
const fixed=(n,d=3)=>Number.isFinite(n)?n.toFixed(d):'—';
const position=p=>p?p.map(v=>fixed(v,2)).join(' / '):'—';
const clock=t=>`${String(Math.floor(t/60)).padStart(2,'0')}:${(t%60).toFixed(3).padStart(6,'0')}`;
function status(message,type=''){$('status').textContent=message;$('status').className=`status ${type}`;}
function option(value,text){const o=document.createElement('option');o.value=value;o.textContent=text;return o;}
function batchName(b){
  const name=experiments[b.experiment]??b.experiment;
  if(b.name.includes('单独运行'))return `${name} · 单独运行 · ${b.runs.length} 场`;
  const m=b.sort.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
  const date=m?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+m[6]))):b.sort;
  return `${name} · ${date} · ${b.runs.length} 场`;
}
function resultName(outcome,engineering){return (engineering==='invalid'?'工程无效 · ':'')+(outcomes[outcome]??outcome);}
function pause(){playing=false;$('play').textContent='▶ 播放';lastAnimation=null;}
function shapeMetricRows(){
  if(!data?.metrics)return [];
  const m=data.metrics;
  return data.kind==='line'?
    [['横向 RMS',m.lateral_rmse_blocks,' 格'],['最大横向误差',m.lateral_max_blocks,' 格'],['终点横向误差',m.final_lateral_error_blocks,' 格'],['视角 RMS',m.yaw_error_rms_degrees,'°'],['最大视角误差',m.yaw_error_max_degrees,'°']]:
    [['半径 RMS',m.radial_rmse_blocks,' 格'],['最大半径误差',m.radial_max_blocks,' 格'],['闭合误差',m.closure_error_blocks,' 格'],['拟合圆心误差',m.fitted_center_error_blocks,' 格'],['拟合半径误差',m.fitted_radius_error_blocks,' 格']];
}
function renderShapeMetrics(){
  const shape=!!data?.metrics;$('shape-card').hidden=!shape;if(!shape)return;
  const list=$('shape-metrics');list.replaceChildren();
  for(const [label,value,unit] of shapeMetricRows()){
    const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value==null?'无法拟合':`${fixed(value,3)}${unit}`;list.append(dt,dd);
  }
}
function drawErrorChart(sampleIndex){
  if(!data?.metrics)return;
  const rect=errorCanvas.getBoundingClientRect(),ratio=devicePixelRatio||1;
  errorCanvas.width=Math.max(1,Math.round(rect.width*ratio));errorCanvas.height=Math.max(1,Math.round(rect.height*ratio));
  errorCtx.setTransform(ratio,0,0,ratio,0,0);errorCtx.clearRect(0,0,rect.width,rect.height);
  const values=data.samples.map(s=>Number.isFinite(s.tracking_error)?s.tracking_error:0),maximum=Math.max(.01,...values);
  const X=i=>8+(rect.width-16)*i/Math.max(1,values.length-1),Y=v=>rect.height-9-(rect.height-20)*v/maximum;
  errorCtx.strokeStyle='#d7e0e9';errorCtx.lineWidth=1;errorCtx.beginPath();errorCtx.moveTo(8,Y(0));errorCtx.lineTo(rect.width-8,Y(0));errorCtx.stroke();
  errorCtx.strokeStyle='#148ed2';errorCtx.lineWidth=1.8;errorCtx.beginPath();values.forEach((v,i)=>i?errorCtx.lineTo(X(i),Y(v)):errorCtx.moveTo(X(i),Y(v)));errorCtx.stroke();
  errorCtx.strokeStyle='#d28a2d';errorCtx.lineWidth=1;errorCtx.beginPath();errorCtx.moveTo(X(sampleIndex),6);errorCtx.lineTo(X(sampleIndex),rect.height-8);errorCtx.stroke();
  errorCtx.fillStyle='#7c8999';errorCtx.font='9px sans-serif';errorCtx.fillText(`${fixed(maximum,2)} 格`,8,9);
}

async function refresh(){
  pause();status('正在查找实验轮次…','loading');
  try{
    const response=await fetch('/api/catalog?refresh=1');if(!response.ok)throw new Error('实验目录读取失败');
    catalog=(await response.json()).batches;
    $('batch').replaceChildren(...catalog.map(b=>option(b.id,batchName(b))));
    if(!catalog.length){status('没有找到可识别的导航实验记录');return;}
    const query=new URL(location.href).searchParams,requested=query.get('run');
    const containing=catalog.find(b=>b.id===query.get('batch')&&b.runs.some(r=>r.id===requested))
      ??catalog.find(b=>b.runs.some(r=>r.id===requested));
    if(containing)$('batch').value=containing.id;
    selectBatch(requested);
  }catch(error){status(error.message,'error');}
}
function selectBatch(preferred){
  const batch=catalog.find(b=>b.id===$('batch').value);if(!batch)return;
  $('run').replaceChildren(...batch.runs.map((r,i)=>option(r.id,`${/^maze_\d+(?:_low|_pits)?$/.test(r.case)?'':String(i+1).padStart(2,'0')+' · '}${r.title??cases[r.case]??r.case} · ${r.seed??'—'} / ${r.history} / ${r.backend}${r.prelook===false?' / 预瞄关':''} · ${resultName(r.outcome,r.engineering)}${r.complete?'':'（未封存）'}`)));
  if(preferred&&batch.runs.some(r=>r.id===preferred))$('run').value=preferred;
  loadRun();
}
async function loadRun(){
  const token=++generation;pause();data=null;knowledge=null;columns=new Map();eventKey=null;stageEvents=[];
  time=0;$('seek').value=0;$('seek').disabled=true;$('elapsed').textContent=clock(0);$('duration').textContent='—';
  for(const id of ['position','velocity','heading','stage-target','planning','distance-value','frame-count','summary','memory-status'])$(id).textContent='—';
  $('time-value').textContent='0.000 s';$('events').replaceChildren();$('reason').textContent='正在载入';$('reason-code').textContent='';
  for(const id of ['key-forward','key-back','key-left','key-right'])$(id).classList.remove('active');
  $('movement-text').textContent='—';$('layer-count').textContent='等待本场观察与记忆';
  $('shape-card').hidden=true;
  $('play').disabled=true;$('empty-state').style.display='flex';$('empty-state').querySelector('p').textContent='正在载入实验记录…';
  currentRun=catalog.flatMap(b=>b.runs).find(r=>r.id===$('run').value);if(!currentRun)return;
  $('scene-title').textContent=currentRun.title??cases[currentRun.case]??currentRun.case;
  $('scene-meta').textContent=`种子 ${currentRun.seed} · ${currentRun.history} · ${currentRun.backend}`;
  $('source').textContent=currentRun.source.slice(0,12)||'未记录';$('record-path').textContent=currentRun.directory;
  $('outcome').textContent='载入中';$('outcome').className='badge';
  status('正在解析实际轨迹与地形记忆，首次读取需要几秒…','loading');draw();
  const url=new URL(location.href);url.searchParams.set('run',currentRun.id);url.searchParams.set('batch',$('batch').value);history.replaceState(null,'',url);
  try{
    for(;;){
      const response=await fetch(`/api/run/${currentRun.id}`);const payload=await response.json();
      if(token!==generation)return;
      if(response.status===202){status(payload.message,'loading');await new Promise(r=>setTimeout(r,800));continue;}
      if(!response.ok)throw new Error(payload.message??'读取失败');
      data=payload;break;
    }
    data.knowledge??=[];data.observations??=[];data.entities??=[];data.changes??=[];data.warnings??=[];
    knowledge=new KnowledgeTimeline(data.knowledge);time=0;renderShapeMetrics();
    $('seek').max=data.duration;$('seek').value=0;$('seek').disabled=false;$('duration').textContent=clock(data.duration);$('play').disabled=false;
    $('empty-state').style.display='none';$('outcome').textContent=resultName(data.summary.outcome,data.summary.engineering);$('outcome').className=`badge ${data.summary.outcome}`;
    $('memory-status').textContent=data.memory.available?`${data.memory.imports_checked} 次导入核对通过${data.memory.retained_in_radius?' · 32 格球内保留旧地形':''}`:data.metrics?'运动测试不显示地形记忆':'不可用（仅显示实际观察）';
    $('memory-layer').disabled=!data.memory.available;
    $('summary').textContent=`${fixed(data.duration)} s / ${fixed(data.summary.path_length,2)} 格`;
    $('map-note').textContent=data.map_note+(data.navigation_volume?' 本场蓝色表示当前获得的通行几何，允许穿过遮挡；不代表识别具体方块。悬停查看独立的身份记录。':' 合并视图以所选高度中任一当前观察为蓝色。')+(data.current_history_batches?' 通行信息在当前覆盖时标为 latest，离开后按 5 秒分批保存历史。':'')+' 同位置阶段目标只显示最近编号，完整记录见右侧列表。';
    $('change-events').replaceChildren();
    for(const event of data.changes??[]){
      for(const [t,label] of [[event.t,'提交变化'],[event.confirmed_t,'首次合法确认']]){
        if(t==null)continue;
        const button=document.createElement('button');button.className='change-event';button.textContent=`${label} · ${fixed(t,3)} s`;
        button.onclick=()=>{pause();time=t;update();};$('change-events').append(button);
      }
    }
    const levels=new Set([data.layout.ground_y,...data.layout.obstacles.map(b=>b.y)]);
    for(const f of data.knowledge)for(const c of f.upsert)levels.add(c[1]);
    $('height').replaceChildren(option('all','地面与墙体 · 按列合并'),...Array.from(levels).sort((a,b)=>a-b).map(y=>option(y,`Y = ${y}${y===data.layout.ground_y?' · 地面':''}`)));
    let previous=null;
    for(const d of data.decisions){const key=JSON.stringify(d.target);if(d.target&&key!==previous){stageEvents.push({t:d.t,target:d.target,index:stageEvents.length+1});previous=key;}}
    status(data.warnings.length?data.warnings.join('；'):`已载入 ${data.samples.length} 个身体采样 · ${data.decisions.length} 次决策 · 数据来自封存记录`,data.warnings.length?'error':'');
    update();
  }catch(error){if(token===generation){status(error.message,'error');$('empty-state').querySelector('p').textContent='该场记录暂不可回放';$('outcome').textContent='读取失败';}}
}

function stateNow(){
  if(!data)return {};
  const sampleIndex=Math.max(0,lastAt(data.samples,time));const sample=data.samples[sampleIndex];
  const decision=data.decisions[lastAt(data.decisions,time)];
  let cells,seen;
  if(data.memory.available){knowledge.seek(time);cells=knowledge.cells;seen=knowledge.seen;}
  else {const obs=data.observations[lastAt(data.observations,time)];cells=new Map((obs?.cells??[]).map(c=>[c.slice(0,3).join(','),c]));seen=new Set(obs?.seen??[]);}
  columns=projectLayers(cells,seen,$('height').value,data.layout.ground_y);
  return {sample,sampleIndex,decision};
}
function update(){
  if(!data)return;
  time=Math.max(0,Math.min(data.duration,time));$('seek').value=time;$('elapsed').textContent=clock(time);
  const {sample,sampleIndex,decision:d}=stateNow();
  $('time-value').textContent=fixed(time)+' s';
  $('distance-value').textContent=fixed(Math.hypot(sample.position[0]-data.goal[0],sample.position[2]-data.goal[2]),2)+' 格';
  $('position').textContent=position(sample.position);
  $('velocity').textContent=fixed(Math.hypot(sample.velocity[0],sample.velocity[2]),4)+(data.metrics?' 格/s':' 格/tick');
  $('heading').textContent=`${fixed(sample.yaw,1)}° / ${fixed(sample.pitch,1)}°`;
  $('stage-target').textContent=position(d?.target);
  $('reason').textContent=decisionReason(d);
  $('reason-code').textContent=d?.reason??'';
  if(Number.isFinite(sample.tracking_error)){
    const reference=data.kind==='line'?'横向误差':'半径误差';
    $('shape-error-now').textContent=`${reference} ${fixed(sample.tracking_error,3)} 格`;
    $('reason-code').textContent+=`${$('reason-code').textContent?' · ':''}${reference} ${fixed(sample.tracking_error,3)} 格`;
    if(Number.isFinite(sample.expected_yaw))$('reason-code').textContent+=` · 目标视角 ${fixed(sample.expected_yaw,1)}°`;
  }
  const wait=waitingText(d?.waiting);if(wait)$('reason-code').textContent+=` · ${wait}`;
  const deadline=controlDeadlineText(d?.navigation_control_deadline);if(deadline)$('reason-code').textContent+=` · ${deadline}`;
  const layers=d?.navigation_layer_timing;
  if(layers?.revision===1)$('reason-code').textContent+=` · 前台 ${(layers.foreground_ns/1e6).toFixed(2)} ms / 后台 ${(layers.background_ns/1e6).toFixed(2)} ms`;
  const corridor=d?.navigation_route_layers;
  if(corridor?.revision===1){
    const span=(corridor.corridor_start==null||corridor.corridor_end==null)?'无':`${corridor.corridor_start}–${corridor.corridor_end}`;
    $('reason-code').textContent+=` · 当前走廊 ${span}${corridor.action_authorized?'，本帧可执行':'，本帧未授权'}`;
  }
  const trajectory=d?.navigation_local_trajectory;
  if(trajectory?.revision===1){
    const names={executable:'可执行',brake:'制动',hold:'保持',blocked:'受阻'};
    $('reason-code').textContent+=` · 局部轨迹：${names[trajectory.status]??trajectory.status}`;
  }
  const impact=d?.navigation_impact;
  if(impact?.revision===1){
    const names={none:'无关',local_only:'仅局部',corridor_changed:'影响当前走廊',global_repair:'需要全局修复',route_opportunity:'出现更好路线机会'};
    $('reason-code').textContent+=` · 地图变化：${names[impact.kind]??impact.kind}`;
  }
  if(d?.waiting?.temporary_candidate)$('reason-code').textContent+=` · 临时候选（尚未采用）：${position(d.waiting.temporary_candidate)}`;
  if(d?.waiting?.formal_checkpoint)$('reason-code').textContent+=` · 正式检查点：${position(d.waiting.formal_checkpoint)}`;
  if(d?.choice_timing){
    const c=d.choice_timing;
    const state=c.adopted_t!=null?'候选已采用':c.eligible_t!=null?'候选已满足采用条件':c.published_t!=null?'首次完整候选已发布':c.requested_t!=null?'已提出路线需求':null;
    if(state)$('reason-code').textContent+=` · ${state}`;
  }
  const reviews=(d?.needs??[]).filter(n=>n.need_id?.startsWith('terrain-review/'));
  if(reviews.length)$('reason-code').textContent+=` · ${reviews.length} 处行动前复查待办`;
  if(d?.exploration){
    const latest=d.exploration.events?.at(-1);
    const message=d.exploration.capacity_limited?'探索记录容量受限':d.lifecycle?.event==='exploration_finished'?'原阶段的调查目的已结束':explorationReasons[latest?.event];
    if(message)$('reason-code').textContent+=` · ${message}`;
    if(d.exploration.revision===2){
      const e=d.exploration;
      $('reason-code').textContent+=` · 记录 ${e.records}（摘要 ${e.compact_records}）· 依赖 ${e.dependencies}`;
      const attempt=d.observation_attempts?.active?.at(-1)??d.observation_attempts?.recent?.at(-1);
      if(attempt){
        const states={prepared:'等待提交',submitted:'已提交',observed:'收到匹配观测',answered:'已获得答案',cancelled:'已撤销',expired:'已到期'};
        $('reason-code').textContent+=` · 观察 ${attempt.attempt_id}：${states[attempt.state]??attempt.state}`;
      }
    }
  }
  if(d?.observation_opportunity){
    const o=d.observation_opportunity;
    const modes={moving:'沿路线补看',stopped:'当前没有可执行前段，停步补看',deferred:'补看暂缓',none:'无需普通补看'};
    $('reason-code').textContent+=` · ${modes[o.mode]??o.mode}`;
  }
  $('planning').textContent=d?.planning_ms==null?'—':fixed(d.planning_ms,2)+' ms';
  if(d?.navigation_purpose?.choice_quality==='heuristic')$('reason-code').textContent+=' · 当前沿临时探索目标行走，后台继续比较';
  if(d?.navigation_purpose?.choice_quality==='refined')$('reason-code').textContent+=' · 当前探索目标已完成详细比较';
  if(d?.navigation_purpose?.pending_quality==='heuristic')$('reason-code').textContent+=' · 已有快速候选，等待路线接入与换路判断';
  if(d?.route_handoff?.active){
    const h=d.route_handoff;
    const checked=d.execution_continuity?.checked_endpoint;
    const endpoint=checked?[checked.x,checked.y,checked.z]:h.endpoint;
    const movement=h.stopping?(h.revision===2?'以已核验终点安排停步':'到有效边界减速'):'沿已检查路段继续';
    $('reason-code').textContent+=` · ${h.revision===2?'沿保留道路':'路线交接'}：${movement} → ${position(endpoint)}`;
  }
  if(!d?.route_handoff?.active&&d?.route_handoff?.reason==='retained_validation_pending')$('reason-code').textContent+=' · 保留道路，等待本轮核验';
  if(!d?.route_handoff?.active&&d?.route_handoff?.reason==='retained_connector_refused')$('reason-code').textContent+=' · 道路保留，当前身体位置暂时接不上';
    if(d?.execution_continuity?.temporary_wait||d?.route_handoff?.waiting)$('reason-code').textContent+=' · 临时等待：后续路线尚未接入';
    const schedule=d?.search_schedule??d?.route_search_scheduling;
    const scheduleText=searchScheduleText(schedule);
    if(scheduleText)$('reason-code').textContent+=' · '+scheduleText;
    if(d?.execution_continuity?.gaze_mode==='stable_wait')$('reason-code').textContent+=' · 保持视角等待后继';
  if(d?.fine_gaze?.enabled){
    const f=d.fine_gaze, names={hold:'保持视角',slow:'慢转调整',normal:'常规视角'};
    if(f.active)$('reason-code').textContent+=` · 精细停步：${names[f.preference]??f.preference}`;
  }
  $('frame-count').textContent=`采样 ${sampleIndex+1} / ${data.samples.length}`;
  if(d?.observation_travel_revision===1&&d?.navigation_purpose?.observation_travel){
    const mode=d.observation_opportunity?.mode;
    $('reason-code').textContent+=mode==='moving'?' · 沿已知道路观察':mode==='stopped'?' · 已知前段结束，停留观察':' · 观察位置与行走终点分开';
  }
  const integration=d?.exploration_integration;
  if(integration){
    const stateNames={pending:'尚未核验',possible:'几何上可能看到',blocked:'被已知墙面挡住'};
    const questions=(integration.references??[]).map(r=>`${r.block.join(',')}：${stateNames[r.state]??'状态未记录'}`);
    if(questions.length)$('reason-code').textContent+=' · 待补信息 '+questions.join('；');
    if(!integration.search_complete&&integration.publications>0)$('reason-code').textContent+=' · 已交付可走路线，其他候选继续计算';
  }
  const detail=$('observation-detail-text');
  detail.replaceChildren();
  const addDetail=text=>{const p=document.createElement('p');p.textContent=text;detail.append(p);};
  if(!integration)addDetail('旧记录未提供细分状态。参考坐标不代表当时正在看这里。');
  else{
    const phases={idle:'准备搜索',known_route:'查找已知道路',potential_connection:'估算未知连接',candidate_comparison:'比较候选路线',connection_check:'检查路线接入',observation_refinement:'寻找更好的观察位置'};
    const states={pending:'尚未完成核验',possible:'有可能看到，仍需真实观察确认',blocked:'已知墙面遮挡，暂不在这里尝试'};
    const modes={none:'没有请求额外观察',moving:'沿已知道路观察',stopped:'在已知路段尽头观察',deferred:'当前不安排观察'};
    const obs=d.navigation_observation;
    if(obs?.mode==='material_prefix')addDetail('正在确认即将经过地面的材质；已知道路仍保留。');
    addDetail(obs?.safety_preempted?'安全需求正在接管视角':modes[d.observation_opportunity?.mode]??'观察执行状态未记录');
    if(obs?.focus)addDetail(`当前关注的信息位置：(${obs.focus.join(', ')})。`);
    if(obs?.reason){
      const why={aligned:'已对准，等待合法采样',answered:'相关问题已有答案',no_gain:'这次观察没有取得所需答案',search_pending:'搜索尚未完成',goal_available:'已找到通往终点的道路'};
      if(why[obs.reason])addDetail(why[obs.reason]+'。');
    }
    const ref=d.navigation_purpose?.observation_target;
    if(ref)addDetail('参考位置：'+position(Array.isArray(ref)?ref:[ref.x,ref.y,ref.z]));
    for(const r of integration.references??[]){
      addDetail(`确认 (${r.block.join(', ')}) 的通行信息：${states[r.state]??'状态未记录'}。`);
      if(r.blocker)addDetail(`遮挡位置：(${r.blocker.join(', ')})。`);
      if(r.current_state&&r.current_state!==r.state)addDetail(`实际身体所在位置：${states[r.current_state]??'状态未记录'}。`);
    }
    if(integration.search?.phase)addDetail('搜索：'+(phases[integration.search.phase]??'计算中')+(integration.search_complete?'；本次工作已结束。':integration.publications>0?'；已交付候选，等待按当前身体检查和选择。':'；尚未交付候选。'));
    if(!ref)addDetail('当前没有有效的观察参考。');
  }
  const m=d?.movement??{};
  $('key-forward').classList.toggle('active',m.forward>0);$('key-back').classList.toggle('active',m.forward<0);
  $('key-left').classList.toggle('active',m.strafe>0);$('key-right').classList.toggle('active',m.strafe<0);
  $('movement-text').textContent=!d?'未记录':!m.forward&&!m.strafe?'释放输入':'移动';
  const observed=Array.from(columns.values()).filter(c=>c.color==='observed').length;
  $('layer-count').textContent=`绘图区：当前${data.navigation_volume?'通行几何':'观察'} ${observed} 列 · 仅记忆 ${columns.size-observed} 列`;
  const change=data.changes?.[0];
  $('change-status').textContent=!change?(data.layout.pit_cells?.length?'静态坑洞 · 斜线区域':''):time<change.t?'变化尚未提交':change.confirmed_t==null||time<change.confirmed_t?'变化已提交 · 尚无合法观察确认':'变化已获合法确认';
  const past=stageEvents.filter(e=>e.t<=time),key=past.map(e=>e.index).join(',')+JSON.stringify(d?.target??null);
  if(key!==eventKey){
    eventKey=key;$('events').replaceChildren();
    if(!past.length){const p=document.createElement('p');p.className='subtle';p.textContent='此刻尚未选择阶段目标';$('events').append(p);}
    for(const e of past){const b=document.createElement('button');b.className='event'+(e===past.at(-1)&&JSON.stringify(e.target)===JSON.stringify(d?.target)?' current':'');
      const n=document.createElement('span');n.textContent=e.index;const p=document.createElement('span');p.textContent=`(${fixed(e.target[0],1)}, ${fixed(e.target[2],1)})`;
      const t=document.createElement('time');t.textContent=fixed(e.t,2)+' s';b.append(n,p,t);b.onclick=()=>{pause();time=e.t;update();};$('events').append(b);}
    $('events').scrollTop=$('events').scrollHeight;
  }
  draw(sample,d,sampleIndex);
  drawErrorChart(sampleIndex);
}
function draw(sample,decision,sampleIndex){
  const rect=canvas.getBoundingClientRect(),ratio=devicePixelRatio||1;
  if(canvas.width!==Math.round(rect.width*ratio)||canvas.height!==Math.round(rect.height*ratio)){canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(rect.height*ratio);}
  ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,rect.width,rect.height);if(!data)return;
  if(!sample){const s=stateNow();sample=s.sample;decision=s.decision;sampleIndex=s.sampleIndex;}
  const b=data.layout.bounds,minX=b.min_x,maxX=b.max_x+1,minZ=b.min_z,maxZ=b.max_z+1;
  const scale=Math.min((rect.width-78)/(maxX-minX),(rect.height-62)/(maxZ-minZ));
  const left=(rect.width-scale*(maxX-minX))/2,top=(rect.height-scale*(maxZ-minZ))/2-3;
  const X=x=>left+(x-minX)*scale,Z=z=>top+(maxZ-z)*scale;
  transform={X,Z,scale,left,top,minX,maxX,minZ,maxZ};
  ctx.fillStyle='#fcfdff';ctx.fillRect(X(minX),Z(maxZ),scale*(maxX-minX),scale*(maxZ-minZ));
  if($('base-layer').checked){
    const terrain=terrainAt(data.layout,data.changes,time,$('height').value);
    for(const o of terrain.walls){
      const x=X(o.x),z=Z(o.z+1);ctx.fillStyle=o.visual_kind==='floating'?'#8b77aa':'#4b5b70';ctx.fillRect(x,z,scale,scale);
      if(o.visual_kind==='floating'&&scale>24){ctx.fillStyle='white';ctx.font='bold 11px "Microsoft YaHei",sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('悬',x+scale/2,z+scale/2);}
    }
    if($('height').value==='all'||Number($('height').value)===data.layout.ground_y){
      for(const p of terrain.pits){
        const x=X(p.x),z=Z(p.z+1);ctx.fillStyle='#e3d8c5';ctx.fillRect(x,z,scale,scale);
        ctx.save();ctx.beginPath();ctx.rect(x,z,scale,scale);ctx.clip();ctx.strokeStyle='#86745c';ctx.lineWidth=1.3;
        for(let k=-scale;k<scale*2;k+=8){ctx.beginPath();ctx.moveTo(x+k,z);ctx.lineTo(x+k-scale,z+scale);ctx.stroke();}ctx.restore();
      }
    }
    ctx.strokeStyle='#ce9225';ctx.lineWidth=2;ctx.setLineDash([4,3]);
    for(const e of terrain.pending){for(const b of e.blocks)ctx.strokeRect(X(b.x)+2,Z(b.z+1)+2,scale-4,scale-4);
      if(e.spawn){const p=e.spawn.position;ctx.beginPath();ctx.arc(X(p[0]),Z(p[2]),scale*.35,0,Math.PI*2);ctx.stroke();}}
    ctx.setLineDash([]);
  }
  for(const c of columns.values()){
    if(c.color==='observed'&&!$('seen-layer').checked||c.color==='memory'&&!$('memory-layer').checked)continue;
    ctx.fillStyle=c.color==='observed'?'rgba(48,157,230,.27)':'rgba(232,99,125,.25)';
    ctx.fillRect(X(c.x),Z(c.z+1),scale,scale);
  }
  ctx.strokeStyle='#d8e2ec';ctx.lineWidth=.7;ctx.beginPath();
  for(let x=minX;x<=maxX;x++){ctx.moveTo(X(x),Z(minZ));ctx.lineTo(X(x),Z(maxZ));}
  for(let z=minZ;z<=maxZ;z++){ctx.moveTo(X(minX),Z(z));ctx.lineTo(X(maxX),Z(z));}ctx.stroke();
  ctx.font='10px "Microsoft YaHei",sans-serif';ctx.fillStyle='#8a9aaf';ctx.textAlign='center';ctx.textBaseline='top';
  const stride=Math.max(1,Math.ceil(24/scale));
  for(let x=minX;x<=maxX;x+=stride)ctx.fillText(x,X(x),Z(minZ)+10);
  ctx.textAlign='right';ctx.textBaseline='middle';for(let z=minZ;z<=maxZ;z+=stride)ctx.fillText(z,X(minX)-10,Z(z));
  ctx.textAlign='left';ctx.fillText('x',X(maxX)+12,Z(minZ)+13);ctx.fillText('z',X(minX)-4,Z(maxZ)-13);
  if($('plan-layer').checked&&data.reference_path?.length>1){
    ctx.strokeStyle='#d28a2d';ctx.lineWidth=1.8;ctx.setLineDash([6,5]);ctx.beginPath();
    data.reference_path.forEach((p,i)=>i?ctx.lineTo(X(p[0]),Z(p[2])):ctx.moveTo(X(p[0]),Z(p[2])));ctx.stroke();ctx.setLineDash([]);
  }
  const handoff=decision?.route_handoff;
  if($('plan-layer').checked&&handoff?.revision!==2&&handoff?.active&&decision.execution_origin&&handoff.endpoint){
    const a=decision.execution_origin,b=handoff.endpoint;
    ctx.strokeStyle='#df9a4b';ctx.lineWidth=2.4;ctx.setLineDash([2,4]);ctx.beginPath();
    ctx.moveTo(X(a[0]),Z(a[2]));ctx.lineTo(X(b[0]),Z(b[2]));ctx.stroke();ctx.setLineDash([]);
  }else if($('plan-layer').checked&&decision?.path?.length>1){ctx.strokeStyle='#df9a4b';ctx.lineWidth=1.6;ctx.setLineDash([5,5]);ctx.beginPath();decision.path.forEach((p,i)=>i?ctx.lineTo(X(p[0]),Z(p[2])):ctx.moveTo(X(p[0]),Z(p[2])));ctx.stroke();ctx.setLineDash([]);}
  ctx.strokeStyle='#148ed2';ctx.lineWidth=2.4;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();
  data.samples.slice(0,sampleIndex+1).forEach((s,i)=>i?ctx.lineTo(X(s.position[0]),Z(s.position[2])):ctx.moveTo(X(s.position[0]),Z(s.position[2])));ctx.stroke();
  const past=stageEvents.filter(e=>e.t<=time);ctx.font='11px "Microsoft YaHei",sans-serif';
  const latestMarkers=new Map(past.map(e=>[JSON.stringify(e.target),e]));
  if($('plan-layer').checked&&decision?.waiting?.formal_checkpoint){
    const p=decision.waiting.formal_checkpoint;ctx.strokeStyle='#cb7c1e';ctx.lineWidth=2;ctx.beginPath();ctx.arc(X(p[0]),Z(p[2]),7,0,Math.PI*2);ctx.stroke();ctx.fillStyle='#9b5a14';ctx.fillText('正式检查点',X(p[0])+9,Z(p[2])+10);
  }
  if($('plan-layer').checked&&decision?.waiting?.temporary_candidate){
    const p=decision.waiting.temporary_candidate,x=X(p[0]),z=Z(p[2]);ctx.strokeStyle='#6f61a8';ctx.lineWidth=1.5;ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x,z-7);ctx.lineTo(x+7,z);ctx.lineTo(x,z+7);ctx.lineTo(x-7,z);ctx.closePath();ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#6f61a8';ctx.fillText('临时候选',x+9,z-9);
  }
  const reference=decision?.navigation_purpose?.observation_target;
  if($('plan-layer').checked&&decision?.observation_travel_revision===1&&reference){
    const p=Array.isArray(reference)?reference:[reference.x,reference.y,reference.z];
    const refs=decision?.exploration_integration?.references;
    const state=refs?.length&&refs.every(r=>r.state==='blocked')?'受遮挡':refs?.some(r=>r.state==='possible')?'可能可见':refs?'待核验':'状态未记录';
    const referenceColor=state==='受遮挡'?'#888888':state==='待核验'?'#a69bb0':'#8653a0';
    ctx.strokeStyle=referenceColor;ctx.lineWidth=1.5;ctx.beginPath();
    ctx.moveTo(X(p[0])-5,Z(p[2]));ctx.lineTo(X(p[0])+5,Z(p[2]));
    ctx.moveTo(X(p[0]),Z(p[2])-5);ctx.lineTo(X(p[0]),Z(p[2])+5);ctx.stroke();
    ctx.fillStyle=referenceColor;ctx.fillText(`观察参考 · ${state}`,X(p[0])+7,Z(p[2])+14);
    if($('observation-details').open){
      for(const r of (decision?.exploration_integration?.references??[]).slice(0,4)){
        ctx.strokeStyle='#8653a080';ctx.lineWidth=1;ctx.setLineDash([2,4]);ctx.beginPath();ctx.moveTo(X(p[0]),Z(p[2]));ctx.lineTo(X(r.block[0]+.5),Z(r.block[2]+.5));ctx.stroke();ctx.setLineDash([]);
      }
    }
  }
  for(const e of latestMarkers.values()){ctx.strokeStyle=e===past.at(-1)&&JSON.stringify(e.target)===JSON.stringify(decision?.target)?'#cb7c1e':'#e1b378';ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(X(e.target[0]),Z(e.target[2]),4.5,0,Math.PI*2);ctx.stroke();ctx.fillStyle='#a87835';ctx.fillText(e.index,X(e.target[0])+7,Z(e.target[2])-7);}
  function marker(p,text,color){ctx.fillStyle='white';ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();ctx.arc(X(p[0]),Z(p[2]),6,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=color;ctx.font='bold 12px sans-serif';ctx.fillText(text,X(p[0])+10,Z(p[2])+2);}
  marker(data.start,'S','#30875d');marker(data.goal,'G','#d35571');
  if($('seen-layer').checked){
    const frame=data.entities?.[lastAt(data.entities??[],time)];
    for(const entity of frame?.items??[]){
      const p=entity.position;
      if($('height').value!=='all'&&(Number($('height').value)+1<=p[1]||Number($('height').value)>=p[1]+entity.size[1]))continue;
      ctx.fillStyle='#b576ce';ctx.strokeStyle='#684081';ctx.lineWidth=1.5;
      const w=entity.size[0]*scale,h=entity.size[2]*scale;
      ctx.fillRect(X(p[0])-w/2,Z(p[2])-h/2,w,h);ctx.strokeRect(X(p[0])-w/2,Z(p[2])-h/2,w,h);
      ctx.fillStyle='#684081';ctx.font='11px "Microsoft YaHei",sans-serif';ctx.fillText(entity.type==='minecraft:villager'?'村民':entity.type.replace('minecraft:',''),X(p[0])+w/2+4,Z(p[2]));
    }
  }
  const angle=sample.yaw*Math.PI/180,dx=-Math.sin(angle),dz=Math.cos(angle),px=X(sample.position[0]),pz=Z(sample.position[2]);
  ctx.strokeStyle='#17415e';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(px,pz);ctx.lineTo(px+dx*22,pz-dz*22);ctx.stroke();
  ctx.fillStyle='#164d6c';ctx.strokeStyle='white';ctx.lineWidth=2;ctx.beginPath();ctx.arc(px,pz,6,0,Math.PI*2);ctx.fill();ctx.stroke();
}
canvas.addEventListener('mousemove',event=>{
  if(!data||!transform)return;
  const rect=canvas.getBoundingClientRect(),mx=event.clientX-rect.left,my=event.clientY-rect.top,t=transform;
  const x=Math.floor((mx-t.left)/t.scale+t.minX),z=Math.floor(t.maxZ-(my-t.top)/t.scale);
  if(x<t.minX||x>=t.maxX||z<t.minZ||z>=t.maxZ){$('tooltip').hidden=true;return;}
  const item=columns.get(`${x},${z}`);
  const records=[...(item?.records??[])].sort((a,b)=>b.cell[1]-a.cell[1]);
  const text=[`格子 (${x}, ${z})`,...records.map(r=>{
    const cell=r.cell,sources=cell[6]??[],air=['minecraft:air','minecraft:cave_air','minecraft:void_air','mc2p:navigation_air'].includes(cell[3]);
    const volume=sources.includes('navigation_volume');
    const identity=volume?knowledge?.visualCells?.get(cell.slice(0,3).join(',')):null;
    const visualTime=identity?.[7];
    const shapeText={empty:'无固体碰撞',full_cube:'完整方块障碍',boxes:'局部形状障碍',unsupported:'形状尚不支持'};
    const origin=volume?(air?'通行信息：空气':`通行信息：${shapeText[cell[4]]??'形状未知'}${cell[5]?'，有流体':''}`)+(identity?` · 身份${knowledge?.visualSeen?.has(cell.slice(0,3).join(','))?'本帧已识别':'仅历史'}：${identity[3].replace('minecraft:','')}${visualTime!=null?`（${fixed(Math.max(0,time-visualTime),1)} 秒前）`:''}`:' · 具体身份未识别'):sources.includes('inferred_air')?'推断空气':air?'直接观察空气':cell[3].replace('minecraft:','');
    const contact=sources.includes('head_contact')?'（记录来源含头部接触）':sources.includes('body_contact')?'（记录来源含身体或脚下接触）':'';
    const protectedText=knowledge?.protected?.has(cell.slice(0,3).join(','))?' · 保留已知非完整方块，本帧空气推断未采纳':'';
    const timeText=terrainTimeText(cell,r.observed,time);
    return `Y=${cell[1]}  ${r.observed?'本帧有证据':'历史记忆，本帧未更新'} · ${origin}${contact}${protectedText}${timeText?` · ${timeText}`:''}`;
  })];
  if(data.memory.available){
    const selected=$('height').value;
    const heights=selected==='all'?[data.layout.ground_y,data.layout.ground_y+1,data.layout.ground_y+2,data.layout.ground_y+3]:[Number(selected)];
    for(const y of heights)if(!records.some(r=>r.cell[1]===y))text.push(`Y=${y}  未知，没有地形记录`);
  }
  if(!item)text.push('所选高度没有观察或记忆记录');
  $('tooltip').textContent=text.join('\n');$('tooltip').hidden=false;$('tooltip').style.left=Math.max(5,Math.min(mx+16,rect.width-250))+'px';$('tooltip').style.top=Math.max(5,Math.min(my+16,rect.height-140))+'px';
});
canvas.addEventListener('mouseleave',()=>{$('tooltip').hidden=true;});
canvas.addEventListener('click',event=>{
  const decision=data?.decisions?.[lastAt(data?.decisions??[],time)],ref=decision?.navigation_purpose?.observation_target;
  if(!ref||!transform)return;
  const p=Array.isArray(ref)?ref:[ref.x,ref.y,ref.z],rect=canvas.getBoundingClientRect(),t=transform;
  if(Math.hypot(event.clientX-rect.left-(t.left+(p[0]-t.minX)*t.scale),event.clientY-rect.top-(t.top+(t.maxZ-p[2])*t.scale))<12){$('observation-details').open=true;update();}
});
$('observation-details').addEventListener('toggle',()=>{if(data)draw();});
function togglePlay(){if(!data)return;if(playing)pause();else{if(time>=data.duration)time=0;playing=true;lastAnimation=null;$('play').textContent='Ⅱ 暂停';}}
function step(direction){if(!data)return;pause();const i=lastAt(data.samples,time);time=data.samples[Math.max(0,Math.min(data.samples.length-1,i+direction))].t;update();}
$('batch').onchange=()=>selectBatch();$('run').onchange=loadRun;$('refresh').onclick=refresh;$('play').onclick=togglePlay;
$('previous').onclick=()=>step(-1);$('next').onclick=()=>step(1);$('restart').onclick=()=>{pause();time=0;update();};
$('seek').oninput=()=>{pause();time=Number($('seek').value);update();};
for(const id of ['base-layer','seen-layer','memory-layer','plan-layer','height'])$(id).onchange=update;
document.addEventListener('keydown',event=>{if(['INPUT','SELECT','TEXTAREA','BUTTON'].includes(event.target.tagName))return;if(event.code==='Space'){event.preventDefault();togglePlay();}if(event.code==='ArrowLeft'){event.preventDefault();step(-1);}if(event.code==='ArrowRight'){event.preventDefault();step(1);}});
new ResizeObserver(()=>data?update():draw()).observe($('map-wrap'));
function animate(now){if(playing&&data){if(lastAnimation!==null)time+=(now-lastAnimation)/1000*Number($('speed').value);lastAnimation=now;if(time>=data.duration){time=data.duration;pause();}update();}requestAnimationFrame(animate);}
requestAnimationFrame(animate);refresh();
