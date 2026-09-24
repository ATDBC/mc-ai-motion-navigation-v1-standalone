"""Per-plan reuse of the existing historical support rule, without renewal."""
from mc2p.skills.navigation_belief import support_admission,HISTORICAL_SUPPORT_NS
from mc2p.skills.point_goal_policy import PointGoalPolicy
from mc2p.skills.navigation_stage_cache import StageCellCache


class StageSupport:
    def __init__(self,terrain,belief,view,now_ns):
        self.terrain,self.belief,self.view,self.now=terrain,belief,view,now_ns
        self.columns={}

    def clean_column(self,grid):
        if grid in self.columns:return self.columns[grid]
        record=self.terrain.get(grid);marker=self.belief.get(grid)
        clean=(record is not None and marker is not None and marker.history==record and not marker.contradicted
            and support_admission(record,self.now,historical=True,high_consequence=False,contradicted=False) is None)
        if clean:
            x,y,z=grid
            # Successful support_admission already proves the floor checks
            # performed by _neighborhood_complete. Check both retained layers.
            for by in (y+1,y+2):
                record=self.terrain.get((x,by,z))
                if record is None:continue
                marker=self.belief.get((x,by,z))
                if (marker is None or marker.history!=record or marker.contradicted
                        or record.block.fluid_id is not None or record.block.collision.kind!='empty'):
                    clean=False;break
        self.columns[grid]=clean
        return clean

    def __call__(self,record,now_ns,*,historical,contradicted,terrain,belief,block,view):
        if now_ns!=self.now or terrain is not self.terrain or belief is not self.belief or view is not self.view:
            raise ValueError('stage support cache escaped its planning frame')
        reason=support_admission(record,now_ns,historical=historical,high_consequence=True,contradicted=contradicted)
        if reason!='uncertain_history' or not historical or now_ns-record.last_seen.request_start_ns>HISTORICAL_SUPPORT_NS:
            return reason
        x,y,z=block
        if view.base.entities_truncated or not all(self.clean_column((x+dx,y,z+dz))
                for dx in (-1,0,1) for dz in (-1,0,1)):
            return reason
        if any(abs(e.position.x-(x+.5))<=1.5+e.size.x/2 and abs(e.position.z-(z+.5))<=1.5+e.size.z/2
               for e in view.base.entities):return reason
        return support_admission(record,now_ns,historical=True,high_consequence=False,contradicted=contradicted)


class StageGeometry(PointGoalPolicy):
    def __init__(self,*,static_history=False):
        super().__init__('C')
        self.static_history=static_history
        self.support=None
        self.cache=StageCellCache(static_history=static_history)

    @property
    def cache_hits(self):return self.cache.hits

    def prepare(self,snapshot,view,now_ns,terrain,belief):
        self._current_view=view
        if self.static_history:
            from mc2p.skills.navigation_terrain_review import static_support_reason
            self.support=static_support_reason
        else:self.support=StageSupport(terrain,belief,view,now_ns)
        self.cache.prepare(snapshot,view,now_ns,belief)

    def _cell_reason(self,cell,floor,now_ns,terrain,belief):
        compute=lambda:super(StageGeometry,self)._cell_reason(cell,floor,now_ns,terrain,belief,support_reason=self.support)
        if self.support is None:return compute()
        return self.cache.reason(cell,floor,self._cell_body(cell,floor),compute)
