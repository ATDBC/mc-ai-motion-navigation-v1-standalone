from pathlib import Path
from dataclasses import asdict
import hashlib, json, subprocess, sys, zipfile
ROOT=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(ROOT))
from scripts.export_motion_navigation_standalone import export_tree, verify_tree, _verify_hashes
BASE="3889d10e298fec57ce7b0a82a63bef331c431bb7"
BROKEN="dfaa0b4f12b3f0cde18f7c3e2b12329e666df4ae"
work=ROOT/".tmp"/"d121-task2-integration"
work.mkdir(exist_ok=True)
base=work/"base"
if not base.exists():
    archive=work/"base.zip"
    subprocess.run(["git","archive","--format=zip",f"--output={archive}",BASE],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(base)
_verify_hashes(base)
raw=export_tree(work/"raw",clean=True)
reports=[]
fixed=export_tree(work/"fixed",clean=True,byte_base_root=base,byte_report_sink=reports.append)
verified=verify_tree(fixed)
paths=lambda tree:{p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
base_paths,raw_paths,fixed_paths=map(paths,(base,raw,fixed))
eol_before=[]; actual_changed=[]; eol_after=[]
for name in sorted(base_paths & raw_paths - {"EXPORT-METADATA.json","SHA256SUMS.txt"}):
    old=(base/name).read_bytes(); new=(raw/name).read_bytes(); actual=(fixed/name).read_bytes()
    try: old.decode("utf-8");new.decode("utf-8")
    except UnicodeDecodeError: equivalent=False
    else: equivalent=b"\0" not in old+new and old.replace(b"\r\n",b"\n")==new.replace(b"\r\n",b"\n")
    if old != new and equivalent:
        eol_before.append(name)
        if actual != old: eol_after.append(name)
    if not equivalent:
        assert actual==new,name
        if old!=new:actual_changed.append(name)
assert not eol_after,eol_after
assert raw_paths==fixed_paths
for name in raw_paths-base_paths:
    assert (raw/name).read_bytes()==(fixed/name).read_bytes(),name
# Count the exact historical published EOL damage independently of this source tree.
broken_zip=work/"published.zip"
subprocess.run(["git","archive","--format=zip",f"--output={broken_zip}",BROKEN],cwd=ROOT,check=True)
historical_eol=[];historical_retained=[];historical_now_real_change=[]
with zipfile.ZipFile(broken_zip) as z:
    for name in sorted(base_paths & set(z.namelist())):
        old=(base/name).read_bytes();new=z.read(name)
        if old!=new and old.replace(b"\r\n",b"\n")==new.replace(b"\r\n",b"\n"):
            historical_eol.append(name)
            if name in fixed_paths and (fixed/name).read_bytes()==old:historical_retained.append(name)
            elif name in fixed_paths and (fixed/name).read_bytes()==(raw/name).read_bytes():historical_now_real_change.append(name)
assert len(historical_retained)+len(historical_now_real_change)==len(historical_eol)
output=ROOT/"evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/task-2"
summary={"base_commit":BASE,"published_commit":BROKEN,"verified_file_count":verified.file_count,"historical_eol_only_changes":len(historical_eol),"historical_restored_exact":len(historical_retained),"historical_with_current_real_change":historical_now_real_change,"candidate_eol_only_changes_before":len(eol_before),"candidate_eol_only_changes_after":len(eol_after),"actual_changed_files_retained":actual_changed,"added":sorted(raw_paths-base_paths),"deleted":sorted(base_paths-raw_paths),"reconciliation_counts":{k:len(v) for k,v in asdict(reports[0]).items()},"metadata_source_commit":json.loads((fixed/"EXPORT-METADATA.json").read_text())["source_commit"]}
(output/"integration.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
(output/"byte-reconciliation.json").write_text(json.dumps(asdict(reports[0]),ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
print(json.dumps(summary,ensure_ascii=False,indent=2))
