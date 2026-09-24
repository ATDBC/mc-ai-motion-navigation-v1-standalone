export function lastAt(rows, time) {
  let lo=0, hi=rows.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(rows[mid].t<=time)lo=mid+1;else hi=mid;}
  return lo-1;
}

export function terrainTimeText(cell, observed, time) {
  const metadata=cell[8];
  if(metadata?.model==='current_history_5s'){
    if(observed&&metadata.current)return '通行信息：当前覆盖（latest）';
    if(metadata.history_batch_start!=null){
      const youngest=Math.max(0,time-metadata.history_batch_start-5);
      const oldest=Math.max(0,time-metadata.history_batch_start);
      return `离开通行覆盖：约 ${youngest.toFixed(1)}—${oldest.toFixed(1)} 秒前（5 秒分批）`;
    }
    return '最近采样曾覆盖，当前帧未确认';
  }
  return cell[7]!=null?`证据距今 ${Math.max(0,time-cell[7]).toFixed(1)} 秒`:'';
}

export function terrainAt(layout,changes,time,height='all'){
  const blocks=new Map(layout.obstacles.map(b=>[`${b.x},${b.y},${b.z}`,b]));
  const pits=new Map((layout.pit_cells??[]).map(p=>[`${p.x},${p.z}`,p]));
  const pending=[];
  for(const event of changes??[]){
    if(event.t>time)continue;
    if(event.confirmed_t==null||event.confirmed_t>time){pending.push(event);continue;}
    for(const b of event.blocks){
      blocks.set(`${b.x},${b.y},${b.z}`,b);
      if(b.y===layout.ground_y){if(b.block==='minecraft:air')pits.set(`${b.x},${b.z}`,b);else pits.delete(`${b.x},${b.z}`);}
    }
  }
  const walls=new Map();
  for(const b of blocks.values())if(b.block!=='minecraft:air'&&(height==='all'?b.y>layout.ground_y:b.y===Number(height)))walls.set(`${b.x},${b.z}`,b);
  return {walls:[...walls.values()],pits:[...pits.values()],pending};
}

export class KnowledgeTimeline {
  constructor(frames){this.frames=frames;this.cells=new Map();this.seen=new Set();this.lastSeen=new Map();this.visualCells=new Map();this.visualSeen=new Set();this.index=-1;this.checkpoints=new Map();}
  seek(time){
    const target=lastAt(this.frames,time);
    if(target<this.index){
      let best=-1;
      for(const i of this.checkpoints.keys())if(i<=target&&i>best)best=i;
      const saved=this.checkpoints.get(best);
      this.cells=new Map(saved?.cells??[]);this.lastSeen=new Map(saved?.lastSeen??[]);
      this.seen=new Set(saved?.seen??[]);this.index=best;
      this.visualCells=new Map(saved?.visualCells??[]);this.visualSeen=new Set(saved?.visualSeen??[]);
    }
    while(this.index<target){
      const f=this.frames[++this.index];
      for(const key of f.remove){this.cells.delete(key);this.lastSeen.delete(key);}
      for(const cell of f.upsert)this.cells.set(cell.slice(0,3).join(','),cell);
      this.seen=new Set(f.seen);
      for(const key of f.seen)this.lastSeen.set(key,f.t);
      for(const key of f.visual_remove??[])this.visualCells.delete(key);
      for(const cell of f.visual_upsert??[])this.visualCells.set(cell.slice(0,3).join(','),cell);
      this.visualSeen=new Set(f.visual_seen??[]);
      if(this.index%100===0&&!this.checkpoints.has(this.index))this.checkpoints.set(this.index,{cells:new Map(this.cells),seen:new Set(this.seen),lastSeen:new Map(this.lastSeen),visualCells:new Map(this.visualCells),visualSeen:new Set(this.visualSeen)});
    }
    this.protected=new Set(this.frames[target]?.protected??[]);
    return this;
  }
}

export function projectLayers(cells,seen,height,ground){
  const columns=new Map();
  for(const [id,c] of cells){
    if(height==='all'?(c[1]<ground||c[1]>ground+3):c[1]!==Number(height))continue;
    const key=`${c[0]},${c[2]}`;
    const item=columns.get(key)??{x:c[0],z:c[2],color:'memory',records:[]};
    const observed=seen.has(id);
    if(observed)item.color='observed';
    item.records.push({cell:c,observed});columns.set(key,item);
  }
  return columns;
}
