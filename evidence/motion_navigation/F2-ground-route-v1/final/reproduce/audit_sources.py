from pathlib import Path
import hashlib,json,sys
ROOT=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
OUT=ROOT/'evidence/motion_navigation/F2-ground-route-v1/final'
def read(p):return json.loads(p.read_text('utf-8'))
def check(entries):
 for name,fingerprint in entries.items():
  assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==fingerprint,name
forward=read(OUT/'windows/forward-final.json')
reverse=read(OUT/'windows/reverse-final.json')
assert forward['production_files_sha256']==reverse['production_files_sha256']
check(forward['production_files_sha256'])
captured=[]
for relative in ('f2/summary.json','performance/ground.json','performance/terminal.json'):
 r=read(OUT/relative);check(r['production']['files'])
 for name,sha in forward['production_files_sha256'].items():
  assert r['production']['files'][name]==sha,name
 captured.append({'path':relative,'production_sha256':r['production']['sha256'],
  'files':len(r['production']['files'])})
for name in ('d058','d061'):
 r=read(OUT/f'performance/{name}-compact.json')
 assert r.get('passed',True) and all(r['gates'].values())
 entries={entry['path']:entry['sha256'] for entry in r['source']['files']}
 check(entries)
 captured.append({'path':f'performance/{name}-compact.json',
  'explicit_dependency_files_verified':entries,'all_original_gates_passed':True})
payload={'passed':True,'capture_commit':'5a78b55dcc9b994731558a0ca16cb0e1460ce66c',
 'capture_worktree_clean':False,'formal_platform':'Windows',
 'forward_tests':forward['tests'],'reverse_tests':reverse['tests'],
 'forward_reverse_production_identical':True,'full_production_files':forward['production_files_sha256'],
 'captures':captured,'boundary':'Actual source bytes, not capture HEAD alone, identify the implementation. Task5 changes are included in delivery commit.'}
(OUT/'source-consistency.json').write_text(json.dumps(payload,indent=2)+'\n','utf-8')
print('Source consistency passed; forward/reverse',forward['tests'],reverse['tests'])
