from pathlib import Path
import json
import subprocess
import sys
import time

root=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
python=str(root/'.venv/python.exe')
commands=[
 ('forward-final',['scripts/run_motion_navigation_checks.py','--output','.tmp/f2-task5-startfix-forward-final.json']),
 ('reverse-final',['scripts/run_motion_navigation_checks.py','--reverse','--output','.tmp/f2-task5-startfix-reverse-final.json']),
 *[(g,['scripts/navigation_s0r_evidence.py','--set',g,'--output',f'.tmp/f2-task5-startfix-{g}','--workers','4'])
   for g in ('faults','world_changes','follow','coordination','product')],
 ('f2-all',['scripts/f2_ground_route_evidence.py','--output','.tmp/f2-task5-startfix-all','--workers','4']),
 ('ground-performance',['evidence/motion_navigation/F2-ground-route-v1/task3/review-fix/microbenchmark.py','.tmp/f2-task5-startfix-ground-performance.json']),
 ('terminal-performance',['evidence/motion_navigation/F2-ground-route-v1/task4/microbenchmark.py','.tmp/f2-task5-startfix-terminal-performance.json']),
 ('d058',['-m','scripts.benchmark_d058_route_revalidation','--output','.tmp/f2-task5-startfix-d058']),
 ('d061',['-m','scripts.benchmark_d061_long_session','--output','.tmp/f2-task5-startfix-d061']),
]
records=[]
for name,arguments in commands:
 print('BEGIN '+name,flush=True)
 begun=time.perf_counter()
 logfile=root/'.tmp'/('f2-task5-startfix-'+name+'.log')
 with logfile.open('wb') as stream:
  result=subprocess.run([python,*arguments],cwd=root,stdout=stream,stderr=subprocess.STDOUT)
 records.append(dict(stage=name,command=[python,*arguments],return_code=result.returncode,
  elapsed_seconds=time.perf_counter()-begun,log=str(logfile)))
 (root/'.tmp/f2-task5-startfix-pipeline.json').write_text(json.dumps(records,indent=2)+'\n','utf-8')
 print('END '+name+' code='+str(result.returncode),flush=True)
 if result.returncode:
  print(logfile.read_text('utf-8',errors='replace')[-5000:],flush=True)
  raise SystemExit(result.returncode)
