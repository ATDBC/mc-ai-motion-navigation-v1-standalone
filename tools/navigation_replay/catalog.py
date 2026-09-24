"""Discover bounded navigation artifact directories without walking game data."""
import hashlib
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def inside(root, path):
    root = Path(root).resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('路径不在实验目录内')
    return candidate


def identity(path):
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:20]


def artifact_roots(root):
    artifacts = Path(root)/'artifacts'
    if not artifacts.is_dir():
        return []
    candidates = [artifacts/'joint-j5']
    candidates += [p/'artifacts/joint-j5' for p in artifacts.iterdir()
                   if p.is_dir() and not p.is_symlink()]
    return [p for p in candidates if p.is_dir()]


def run_info(root, directory, row=None):
    directory = inside(root, directory)
    row = row or {}
    evidence = directory/'evidence'
    if not (evidence/'run-manifest.json').is_file() and not row:
        return None
    manifest = read_json(evidence/'run-manifest.json') if (evidence/'run-manifest.json').exists() else {}
    job = row.get('job') or (read_json(directory/'job.json') if (directory/'job.json').exists() else {})
    case = manifest.get('case_plan', {})
    result = {}
    if (directory/'evidence-result.json').exists():
        # The full evaluator report may be large; the matrix row is sufficient.
        if not row:
            result = read_json(directory/'evidence-result.json')
    relative = directory.relative_to(root).as_posix()
    return dict(id=identity(relative), directory=relative, case=job.get('case', case.get('case','未知场景')),
                seed=job.get('seed',case.get('seed')), history=job.get('history',manifest.get('history','H0')),
                backend=job.get('backend',manifest.get('backend','未知')),
                prelook=job.get('prelook_enabled',manifest.get('joint_configuration',{}).get('prelook_enabled')),
                source=manifest.get('source_archive',{}).get('tree_sha256',''),
                outcome=row.get('outcome',result.get('algorithm_outcome','未记录')),
                engineering=row.get('engineering',result.get('engineering_status','未记录')),
                complete=(evidence/'terminal.json').exists() and (evidence/'runtime-trace/trace/complete.json').exists(),
                name=directory.name, strategy=job.get('group',manifest.get('group','')))


def motion_shape_batches(root):
    """Expose already-normalized B03 shape evidence without walking game data."""
    base=Path(root)/'artifacts/fabric-deployment'
    if not base.is_dir():
        return []
    batches=[]
    for directory in sorted(base.iterdir(),reverse=True):
        result_path=directory/'result.json'
        if not directory.is_dir() or directory.is_symlink() or not result_path.is_file():
            continue
        try:
            result=read_json(result_path)
            if result.get('scenario')!='b03-shape-tracking':
                continue
            sources=result.get('core_sources_before',{})
            source=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            runs=[]
            for data_path in sorted(directory.glob('client-*/b03-shape-trials.json')):
                evidence=read_json(data_path)
                for index,scenario in enumerate(evidence.get('scenarios',())):
                    case=scenario.get('case',f'shape-{index}')
                    relative=directory.relative_to(root).as_posix()
                    data_relative=data_path.relative_to(root).as_posix()
                    summary=scenario.get('summary',{})
                    runs.append(dict(
                        id=identity(f'{data_relative}#{case}'),directory=relative,
                        data_file=data_relative,scenario_index=index,kind='motion_shape',
                        case=case,title=scenario.get('title',case),seed=result.get('seed'),
                        history='固定路线',backend='Fabric',prelook=None,source=source,
                        outcome=summary.get('outcome',scenario.get('state','未记录')),
                        engineering=summary.get('engineering','valid'),complete=True,
                        name=f'{directory.name}:{case}',strategy='B03 补充运动测试'))
            if runs:
                relative=directory.relative_to(root).as_posix()
                batches.append(dict(id=identity(relative+'#shape'),
                    name='B03 运动形状 · '+directory.name,directory=relative,
                    experiment='B03 运动形状',sort=directory.name,runs=runs))
        except (ValueError,OSError,KeyError,TypeError):
            continue
    return batches


def known_map_batches(root):
    """Expose the three B04 obstacle runs as interactive replays."""
    base=Path(root)/'artifacts/fabric-deployment'
    if not base.is_dir():
        return []
    titles={'wall':'自动绕行 · 两格高墙',
            'floating':'自动绕行 · 第二层悬浮墙',
            'pit':'自动绕行 · 地面坑洞'}
    batches=[]
    for directory in sorted(base.iterdir(),reverse=True):
        result_path=directory/'result.json'
        if not directory.is_dir() or directory.is_symlink() or not result_path.is_file():
            continue
        try:
            result=read_json(result_path)
            if result.get('scenario')!='b04-known-map':
                continue
            sources=result.get('core_sources_before',{})
            source=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            runs=[]
            for data_path in sorted(directory.glob('client-*/b04-known-map.json')):
                evidence=read_json(data_path)
                for index,scenario in enumerate(evidence.get('scenarios',())):
                    case=scenario.get('name')
                    if case not in titles:
                        continue
                    relative=directory.relative_to(root).as_posix()
                    data_relative=data_path.relative_to(root).as_posix()
                    succeeded=scenario.get('planning_status')=='complete' and bool(scenario.get('decisions')) and scenario['decisions'][-1].get('state')=='succeeded'
                    runs.append(dict(
                        id=identity(f'{data_relative}#{case}'),directory=relative,
                        data_file=data_relative,scenario_index=index,kind='known_map',
                        case=case,title=titles[case],seed=result.get('seed'),
                        history='完整已知地图',backend='Fabric',prelook=None,source=source,
                        outcome='success' if succeeded else 'failed',engineering='valid',
                        complete=True,name=f'{directory.name}:{case}',strategy='B04 已知地图自动绕行'))
            if runs:
                relative=directory.relative_to(root).as_posix()
                batches.append(dict(id=identity(relative+'#known-map'),
                    name='B04 自动绕行 · '+directory.name,directory=relative,
                    experiment='B04 自动绕行',sort=directory.name,runs=runs))
        except (ValueError,OSError,KeyError,TypeError):
            continue
    return batches


def discover(root):
    root = Path(root).resolve()
    batches = []
    for base in artifact_roots(root):
        experiment = '主线实验' if base == root/'artifacts/joint-j5' else base.parents[1].name
        included = set()
        for batch in sorted(base.glob('matrix-*'), reverse=True):
            if not (batch/'results.json').exists():
                continue
            try:
                rows = read_json(batch/'results.json')
            except (ValueError, OSError):
                continue
            runs = []
            for row in rows:
                try:
                    directory = inside(base, row['directory'])
                    info = run_info(root,directory,row)
                    if info:
                        runs.append(info)
                        included.add(directory)
                except (ValueError, OSError, KeyError):
                    continue
            if runs:
                relative = batch.relative_to(root).as_posix()
                batches.append(dict(id=identity(relative), name=experiment+' · '+batch.name.removeprefix('matrix-'),
                                    directory=relative, experiment=experiment, sort=batch.name.removeprefix('matrix-'), runs=runs))
        standalone = []
        for directory in sorted(base.iterdir(), reverse=True):
            if not directory.is_dir() or directory.resolve() in included or directory.name.startswith('matrix-'):
                continue
            try:
                info = run_info(root,directory)
                if info:
                    standalone.append(info)
            except (ValueError, OSError, KeyError):
                continue
        if standalone:
            batches.append(dict(id=identity(str(base)+'single'), name=experiment+' · 单独运行',
                                directory=base.relative_to(root).as_posix(), experiment=experiment,
                                sort=standalone[0]['name'], runs=standalone))
    batches.extend(motion_shape_batches(root))
    batches.extend(known_map_batches(root))
    return sorted(batches,key=lambda b:b['sort'],reverse=True)
