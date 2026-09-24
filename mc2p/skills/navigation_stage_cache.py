"""Local admission cache keyed by decision facts, never observation renewal."""
from itertools import product
import math

from mc2p.skills.terrain_spatial_index import TILE_SIZE, box_tiles


class StageCellCache:
    def __init__(self,*,static_history=False):
        self.static_history=static_history
        self.clear()

    def clear(self):
        self.facts={}
        self.entries={}
        self.edges={}
        self.entities=None
        self.binding=None
        self.changed=set()
        self.hits=self.misses=0
        self.edge_hits=0

    def prepare(self,snapshot,view,now_ns,belief):
        own=view.base.own.position
        cx,cz=math.floor(own.x),math.floor(own.z)
        floor=round(own.y)-1
        index=snapshot.terrain_index
        binding=(snapshot.scope_id,snapshot.latest.stamp.scope,floor)
        if binding!=self.binding:
            self.clear();self.binding=binding
        # Include the neighbor columns used by historical admission, and every
        # collision shape touching this window even when its owner is outside.
        minimum=(cx-10,floor,cz-10);maximum=(cx+10,floor+2,cz+10)
        positions=set(index.positions_in_grid(minimum,maximum))
        tiles=product(*(range(a//TILE_SIZE,b//TILE_SIZE+1) for a,b in zip(minimum,maximum)))
        for tile in tiles:positions.update(index.collisions.get(tile,()))
        positions.update(index.overflow)
        facts={}
        for position in positions:
            record=index.get(position);marker=belief.get(position)
            block=record.block
            age=now_ns-record.last_seen.request_start_ns
            band=-1 if age<0 else 0 if age<=500_000_000 else 1 if age<=60_000_000_000 else 2
            if self.static_history and band==1:band=0
            facts[position]=(block.collision,block.fluid_id,band,
                marker is not None,marker is not None and marker.history==record,
                marker is not None and marker.contradicted,block.block_id,
                marker.review_after_ns if self.static_history and marker is not None else None)
        changed={p for p in self.facts.keys()|facts.keys() if self.facts.get(p)!=facts.get(p)}
        entities=(view.base.entities_truncated,tuple((e.position,e.size) for e in view.base.entities))
        collision_tiles=set();overflow=False
        for p in changed:
            old,new=self.facts.get(p),facts.get(p)
            if old is not None and new is not None and old[:2]+old[3:]==new[:2]+new[3:]:
                continue  # Evidence age affects support dependencies, not boxes.
            for value in (self.facts.get(p),facts.get(p)):
                if value is None:continue
                collision=value[0]
                # Collision boxes in ObservedBlockV3 are block-local.
                from mc2p.contracts.observation_v3 import AabbV3
                boxes=(AabbV3(0,0,0,1,1,1),) if collision.kind in ('unsupported','full_cube') else collision.boxes
                if value[1] is not None:boxes=boxes+(AabbV3(0,0,0,1,1,1),)
                for b in boxes:
                    cells=box_tiles(AabbV3(b.min_x+p[0],b.min_y+p[1],b.min_z+p[2],
                                         b.max_x+p[0],b.max_y+p[1],b.max_z+p[2]))
                    if cells is None:overflow=True
                    else:collision_tiles.update(cells)
        if entities!=self.entities or overflow:
            self.entries.clear()
        elif changed:
            self.entries={key:entry for key,entry in self.entries.items()
                if not entry[1].intersection(changed) and not entry[2].intersection(collision_tiles)}
        self.entries={key:entry for key,entry in self.entries.items()
            if abs(key[0]-cx)<=9 and abs(key[1]-cz)<=9 and key[2]==floor}
        self.edges={key:entry for key,entry in self.edges.items() if not overflow
            and key[4]==own.y
            and not entry[1].intersection(collision_tiles)
            and all(abs(x-cx)<=9 and abs(z-cz)<=9 for x,z in ((key[0],key[1]),(key[2],key[3])))}
        self.facts=facts;self.entities=entities;self.changed=changed
        self.center=(cx,cz,floor)
        self.hits=self.misses=0
        self.edge_hits=0

    def edge(self,a,b,height,body,compute):
        first,last=sorted((a,b));key=(*first,*last,height)
        cx,cz,_=self.center
        if any(abs(x-cx)>9 or abs(z-cz)>9 for x,z in (a,b)):return compute()
        if key in self.edges:
            self.edge_hits+=1
            return self.edges[key][0]
        value=compute()
        self.edges[key]=(value,frozenset(box_tiles(body)))
        return value

    def cached_edge(self,a,b,height):
        """Return a current prepared edge result, without constructing its box."""
        first,last=sorted((a,b));key=(*first,*last,height)
        value=self.edges.get(key)
        if value is None:return None
        self.edge_hits+=1
        return value[0]

    def reason(self,cell,floor,body,compute):
        key=(*cell,floor)
        cx,cz,cy=self.center
        if floor!=cy or abs(cell[0]-cx)>9 or abs(cell[1]-cz)>9:
            self.misses+=1
            return compute()
        if key in self.entries:
            self.hits+=1
            return self.entries[key][0]
        self.misses+=1
        result=compute()
        deps=(frozenset(((cell[0],floor,cell[1]),)) if self.static_history else
            frozenset((cell[0]+dx,floor+dy,cell[1]+dz)
                for dx in (-1,0,1) for dz in (-1,0,1) for dy in (0,1,2)))
        self.entries[key]=(result,deps,frozenset(box_tiles(body)))
        return result
