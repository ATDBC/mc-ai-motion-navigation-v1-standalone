import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class ReplayTests(unittest.TestCase):
    def test_current_terrain_keeps_source_time_separate_from_history_batch(self):
        from types import SimpleNamespace as Obj
        module=self.api('extract')
        block=Obj(position=(1,2,3),block_id='mc2p:navigation_air',
                  collision=Obj(kind='empty'),fluid_id=None,sources=('navigation_volume',))
        source=Obj(request_start_ns=4_990_000_000)
        current=Obj(block=block,last_seen=source,current=True,history_batch_ns=None)
        row=module.terrain_record(current,lambda ns:ns/1e9)
        self.assertEqual(row[7],4.99)
        self.assertEqual(row[8],dict(model='current_history_5s',current=True,history_batch_start=None))
        history=Obj(block=block,last_seen=source,current=False,history_batch_ns=5_000_000_000)
        old=module.terrain_record(history,lambda ns:ns/1e9)
        self.assertEqual(old[7],4.99,'退出批次不能伪装成较新的观察来源')
        self.assertEqual(old[8]['history_batch_start'],5)
        self.assertEqual(module.memory_delta({'a':row},{'a':row})['upsert'],[])
        self.assertEqual(module.memory_delta({'a':row},{'a':old})['upsert'],[old])
        self.assertEqual(len(module.terrain_record(Obj(block=block,last_seen=source),lambda ns:ns/1e9)),8)

    def test_waiting_diagnostic_preserves_new_facts_and_does_not_invent_old_ones(self):
        module=self.api('extract')
        self.assertIsNone(module.waiting_record({}))
        diagnostic={'navigation_waiting':dict(revision=1,category='computation',
            reason='candidate_admission_budget',temporary_candidate={'x':2.5,'y':64,'z':3.5},
            formal_checkpoint=[1.5,64,1.5])}
        self.assertEqual(module.waiting_record(diagnostic),dict(revision=1,category='computation',
            reason='candidate_admission_budget',temporary_candidate=[2.5,64,3.5],
            formal_checkpoint=[1.5,64,1.5]))
        self.assertIsNone(module.search_schedule_record({}))
        schedule=dict(revision=2,mode='urgent_route',budget_ns=2_000_000,required=False,
            urgent=True,foreground_claimed=False,idle_claimed=True,skipped_reason='none',
            control_reserve_ns=3_000_000,final_reserve_ns=1_000_000,
            costs_ns={'idle_search_ns':1200})
        self.assertIs(module.search_schedule_record({'navigation_search_schedule':schedule}),schedule)
        idle=dict(decision_ns=100,mode='required_route',budget_ns=8_000_000,
            elapsed_ns=8_200_000,required=True,urgent=True)
        schedule.update(mode='none',budget_ns=0,previous_idle=idle)
        projected=module.search_schedule_record({'navigation_search_schedule':schedule})
        self.assertEqual(projected['mode'],'none')
        self.assertEqual(projected['previous_idle'],idle)
        self.assertIsNone(module.choice_timing_record({},1_000_000_000))
        timing=dict(publication=['choice',3],requested_ns=1_100_000_000,
            published_ns=1_200_000_000,eligible_ns=None,adopted_ns=None)
        self.assertEqual(module.choice_timing_record({'navigation_choice_timing':timing},1_000_000_000),
            dict(publication=['choice',3],requested_t=.1,published_t=.2,eligible_t=None,adopted_t=None))

    def test_change_submission_does_not_claim_confirmation(self):
        module=self.api('extract')
        change=dict(case='terrain_removed',submitted_at_ns=2000000000,commands=['setblock 7 -61 8 minecraft:air'])
        event=module.change_event(change,None,1000000000)
        self.assertEqual(event['t'],1)
        self.assertIsNone(event['confirmed_t'])
        self.assertEqual(event['blocks'][0]['y'],-61)
        report=dict(case='terrain_removed',submitted_at_ns=2000000000,first_legal_change_ns=2800000000)
        self.assertEqual(module.change_event(change,report,1000000000)['confirmed_t'],1.8)
        del report['case']
        self.assertEqual(module.change_event(change,report,1000000000)['confirmed_t'],1.8)
        report['submitted_at_ns']=1
        self.assertIsNone(module.change_event(change,report,1000000000)['confirmed_t'])

    def test_visible_entity_uses_relative_position_and_disappears(self):
        module=self.api('extract')
        raw=dict(self_state=dict(status='valid',value=dict(position=dict(x=7.5,y=-60,z=4.5))),
                 perception=dict(status='valid',value=dict(visible_entities=[dict(track_id='a',entity_type='minecraft:villager',relative_position=dict(x=.1,y=0,z=4),bounding_box_size=dict(x=.6,y=1.95,z=.6))])))
        self.assertEqual(module.visible_entities(raw)[0]['position'],[7.6,-60,8.5])
        raw['perception']['value']['visible_entities']=[]
        self.assertEqual(module.visible_entities(raw),[])

    def api(self, name):
        spec = importlib.util.find_spec(name)
        self.assertIsNotNone(spec, '回放模块尚未实现：' + name)
        return __import__(name)

    def test_catalog_discovers_matrix_and_unbatched_without_duplicate_runs(self):
        module = self.api('catalog')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'artifacts/joint-j5'
            run = base / '20260912T100000Z-run'
            (run / 'evidence').mkdir(parents=True)
            manifest = {'case_plan': {'case': 'empty', 'seed': 1}, 'source_archive': {'tree_sha256': 'a'*64}, 'group': 'goal_directed_exploration'}
            (run/'evidence/run-manifest.json').write_text(json.dumps(manifest))
            (run/'evidence/terminal.json').write_text('{}')
            batch = base/'matrix-20260912T100000Z'
            batch.mkdir()
            (batch/'results.json').write_text(json.dumps([{'directory': str(run), 'job': {'case': 'empty', 'seed': 1}, 'engineering': 'valid', 'outcome': 'success'}]))
            rows = module.discover(root)
            self.assertEqual(sum(len(b['runs']) for b in rows), 1)
            self.assertEqual(rows[0]['runs'][0]['case'], 'empty')

    def test_catalog_and_store_expose_sealed_motion_shape_scenarios(self):
        catalog=self.api('catalog')
        server=self.api('server')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            run=root/'artifacts/fabric-deployment/20260919T100000Z-shape'
            client=run/'client-0';client.mkdir(parents=True)
            scenario=dict(schema_version='mc2p.motion-shape-replay.v1',
                case='shape_line_rotating_view',title='平视直行',kind='line',
                summary=dict(outcome='success',engineering='valid'),samples=[dict(t=0)],
                decisions=[],memory=dict(available=False),layout={},start=[0,0,0],goal=[0,0,1])
            shape=dict(schema_version='mc2p.b03-shape-trials.v1',scenarios=[scenario])
            (client/'b03-shape-trials.json').write_text(json.dumps(shape),encoding='utf-8')
            result=dict(status='passed',scenario='b03-shape-tracking',seed=21001,
                        core_sources_before={'mc2p/motion_nav/fixed_route.py':'a'*64})
            (run/'result.json').write_text(json.dumps(result),encoding='utf-8')
            rows=catalog.discover(root)
            shape_runs=[item for batch in rows for item in batch['runs']
                        if item.get('kind')=='motion_shape']
            self.assertEqual(len(shape_runs),1)
            self.assertEqual(shape_runs[0]['title'],'平视直行')
            store=server.Store(root)
            try:
                ready=store.get(shape_runs[0]['id'])
                self.assertEqual(ready['status'],'ready')
                self.assertEqual(json.loads(ready['path'].read_text('utf-8'))['case'],
                                 'shape_line_rotating_view')
            finally:
                store.executor.shutdown(wait=True)

    def test_path_cannot_escape_root(self):
        module = self.api('catalog')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                module.inside(Path(folder), '../outside')

    def test_failed_launch_without_manifest_stays_in_catalog(self):
        module=self.api('catalog')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            info=module.run_info(root,root/'artifacts/joint-j5/failed',{'job':{'case':'empty'},'engineering':'invalid','outcome':'failed'})
            self.assertFalse(info['complete'])
            self.assertEqual(info['engineering'],'invalid')

    def test_delta_removes_forgotten_cells_and_preserves_updates(self):
        module = self.api('extract')
        old = {'1,2,3': [1,2,3,'stone',0], '4,5,6': [4,5,6,'stone',0]}
        new = {'4,5,6': [4,5,6,'air',1]}
        delta = module.memory_delta(old, new)
        self.assertEqual(delta['remove'], ['1,2,3'])
        self.assertEqual(delta['upsert'], [[4,5,6,'air',1]])

    def test_missing_stream_is_not_empty_success(self):
        module = self.api('extract')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises((ValueError, FileNotFoundError)):
                list(module.read_stream(Path(folder)))

    def test_sealed_stream_checks_hash_and_count(self):
        module=self.api('extract')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=b'{"record_type":"sample"}\n'
            (root/'segment-00000000.jsonl').write_bytes(raw)
            entry=dict(index=0,filename='segment-00000000.jsonl',byte_count=len(raw),record_count=1,
                       first_record_ordinal=0,last_record_ordinal=0,sha256=hashlib.sha256(raw).hexdigest())
            manifest=(json.dumps(entry)+'\n').encode()
            (root/'manifest.jsonl').write_bytes(manifest)
            (root/'complete.json').write_text(json.dumps(dict(record_count=1,byte_count=len(raw),segment_count=1,
                manifest_sha256=hashlib.sha256(manifest).hexdigest())))
            self.assertEqual(list(module.read_stream(root)),[{'record_type':'sample'}])
            (root/'segment-00000000.jsonl').write_bytes(raw.replace(b'sample',b'broken'))
            with self.assertRaisesRegex(ValueError,'损坏'):
                list(module.read_stream(root))

    def test_proxy_host_requires_explicit_allowance(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from types import SimpleNamespace
        module=self.api('server')
        domain='viewer.example.ts.net'
        for allowed in ((),(domain,)):
            store=SimpleNamespace(catalog=lambda **kwargs: [])
            server=ThreadingHTTPServer(('127.0.0.1',0),module.handler(store,allowed))
            thread=Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                for host,expected in [('localhost',200),('127.0.0.1',200),
                    (domain,200 if allowed else 403),
                    (domain.upper()+':443',200 if allowed else 403),
                    (domain+'.evil.test',403),('other.example.ts.net',403)]:
                    with self.subTest(allowed=allowed,host=host):
                        connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                        try:
                            connection.request('GET','/api/catalog',headers={'Host':host})
                            response=connection.getresponse()
                            self.assertEqual(response.status,expected)
                            body=json.loads(response.read())
                            if expected==200:self.assertEqual(body,{'batches':[]})
                        finally:connection.close()
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_cache_fingerprint_changes_with_memory_record(self):
        module=self.api('server')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'memory-observations.json').write_text('[]')
            before=module.fingerprint(root)
            (root/'memory-observations.json').write_text('[1]')
            self.assertNotEqual(before,module.fingerprint(root))

    def test_rejected_air_marks_only_preserved_non_air_history(self):
        module=self.api('extract')
        raw=[dict(position=[1,2,3],sources=['inferred_air']),
             dict(position=[2,2,3],sources=['inferred_air'])]
        current={'1,2,3':[1,2,3,'minecraft:stone_slab'],
                 '2,2,3':[2,2,3,'minecraft:air']}
        self.assertEqual(module.protected_inferences(raw,current,set()),['1,2,3'])
        self.assertEqual(module.protected_inferences(raw,current,{'1,2,3'}),[])


if __name__ == '__main__':
    unittest.main()
