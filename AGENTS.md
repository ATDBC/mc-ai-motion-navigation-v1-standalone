# AGENTS.md

���ļ������������ֿ⣬ֻ��Ϊ��Ŀ��ڡ���˵���ȶ�ʲô����ǰ�����������������Щ���̱߽磬�Լ��������е�ǰ��顣����ܹ����׶η�Χ�����ս������Ʊ���ֱ�ά���ڶ�ӦĿ¼�У��������︴��һ�ݡ��󷽰�����

## 1. ��ĿĿ��

��Ŀ����Ҫ��һ���������� 2P ���һ����פ��ʵ Minecraft �������Ļ����˻�顣��Ӧ������ָ��𲽾߱��ƶ���ս�����ڿ�ͽ��������������

��ǰ�Ƚ�����Ҽ�����ִ�е������ѽṹ����ͼ��ɺϷ������������жϡ��ɻָ�����������ж����Ƚ����ɿ��Ĺ�����ߣ��ٿ���ģ��ѧϰ��ǿ��ѧϰ��LLM ���е�Լ 20 Hz ����֡���ơ�

## 2. �˶��������ĵ����

�˶��뵼���ع�ֻά�����������ĵ���

| Ŀ¼ | ����ʲô | ʲôʱ����� |
|---|---|---|
| [architecture](docs/motion_navigation/architecture/) | ģ��ְ�𡢽ӿڡ�״̬�����ͳ��ڲ����� | ְ��򹫹��ӿڷ����仯ʱ |
| [stages](docs/motion_navigation/stages/) | ��ǰ�׶Ρ�������Χ�����������ͽ�����һ�׶ε����� | �׶ο�ʼ����Χ�仯�����ʱ |
| [acceptance](docs/motion_navigation/acceptance/) | ���������ָ�ꡢʵ������֤�ݱ߽� | ÿ����ʽ����ʱ |
| [decisions](docs/motion_navigation/decisions/) | Ϊʲô�ı���ơ�ʵʩ˳��������ż� | ������Ҫ���ڱ�����ȡ��ʱ |

��ʼ����ʱ���ȶ����µ� `stages` �ĵ��Ͷ�Ӧ�� `acceptance` �ĵ����ٰ������ȡ��� `architecture` �� `decisions`����ҪĬ�ϼ��ؾ�ʵ����־��Ҳ��Ҫ�Ѿɹ�����־�еġ���ǰ״̬����������Ҫ��

���롢���Ժ�ԭʼ����֤����ʵ��״̬����ʵ��Դ���ĵ�����ʵ��ͻʱ���Ȳ���ԭ�������ĵ�������Ϊ�˷����ĵ�����д��ɾ��ʧ��֤�ݡ�

## 3. ��ǰ�׶�

B01�����������ͳһ�ȽϿھ����� B10���������������ִ�С��Ѿ���ɡ�B10-C ����ɹ滮���ɡ����н��������Ĭ�Ϻ�̨Э���������������������ġ�C1-A���̶��ɼ�Ŀ��ĵ��ν�ս����C1-B���ƶ�Ŀ��������ս���� C1-C����ʵ�ܻ���Ķ���������·�߻ָ������������ʽ Fabric ���գ�

- [�׶μ�¼](docs/motion_navigation/stages/B01-reference-baselines.md)
- [���ս��](docs/motion_navigation/acceptance/B01-reference-baselines.md)
- [B02 �׶μ�¼](docs/motion_navigation/stages/B02-unified-data-geometry.md)
- [B02 ���ս��](docs/motion_navigation/acceptance/B02-world-knowledge-geometry-core.md)
- [B03 �׶μ�¼](docs/motion_navigation/stages/B03-fixed-route-walk.md)
- [B03 ���ռ�¼](docs/motion_navigation/acceptance/B03-fixed-route-walk.md)
- [B04 �׶μ�¼](docs/motion_navigation/stages/B04-known-map-background-planning.md)
- [B04 ���ռ�¼](docs/motion_navigation/acceptance/B04-known-map-background-planning.md)
- [B05 �׶μ�¼](docs/motion_navigation/stages/B05-minimal-height-transition.md)
- [B05 ���ս��](docs/motion_navigation/acceptance/B05-minimal-height-transition.md)
- [B06 ���ս��](docs/motion_navigation/acceptance/B06-version-contracts-ordinary-materials.md)
- [B07 �׶μ�¼](docs/motion_navigation/stages/B07-support-surfaces-shapes-steps.md)
- [B08 �׶μ�¼](docs/motion_navigation/stages/B08-ground-modes-low-clearance.md)
- [B07 ���ս��](docs/motion_navigation/acceptance/B07-support-surfaces-shapes-steps.md)
- [B08 ���ս��](docs/motion_navigation/acceptance/B08-ground-modes-low-clearance.md)
- [B09 �׶μ�¼](docs/motion_navigation/stages/B09-parameterized-air-transitions.md)
- [B09 ���ս��](docs/motion_navigation/acceptance/B09-parameterized-air-transitions.md)
- [B09-R �׶μ�¼](docs/motion_navigation/stages/B09R-physics-calculator.md)
- [B09-R ���ս��](docs/motion_navigation/acceptance/B09R-physics-calculator.md)
- [B10-A �׶μ�¼](docs/motion_navigation/stages/B10A-online-motion-foundation.md)
- [B10-A ���ս��](docs/motion_navigation/acceptance/B10A-online-motion-foundation.md)
- [B10�CB15 �޶��ƻ�](docs/motion_navigation/stages/B10-B15-revised-delivery.md)
- [B10-B �׶μ�¼](docs/motion_navigation/stages/B10B-single-action-solving.md)
- [B10-B ���ս��](docs/motion_navigation/acceptance/B10B-single-action-solving.md)
- [B10-C �׶μ�¼](docs/motion_navigation/stages/B10C-planning-continuous-execution.md)
- [B10 ���ռƻ�](docs/motion_navigation/acceptance/B10-motion-solving-continuous-execution.md)
- [C1 �׶μ�¼](docs/motion_navigation/stages/C1-fixed-visible-melee.md)
- [C1-A ���ս��](docs/motion_navigation/acceptance/C1A-fixed-visible-melee.md)
- [C1-B �׶μ�¼](docs/motion_navigation/stages/C1B-moving-target-melee.md)
- [C1-B ���ս��](docs/motion_navigation/acceptance/C1B-moving-target-melee.md)
- [C1-C �׶μ�¼](docs/motion_navigation/stages/C1C-external-motion-recovery.md)
- [C1-C ���ռƻ�](docs/motion_navigation/acceptance/C1C-external-motion-recovery.md)
- [������ʵʱԭ������](docs/motion_navigation/acceptance/B02-navigation-layer-live-prototype.md)
- [��ǰ�ܹ�](docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md)
- [������۲�ԭ��](docs/motion_navigation/architecture/navigation-layer-observation-v1.md)
- [�̶�·�߲��мܹ�](docs/motion_navigation/architecture/fixed-route-walk-v1.md)
- [��֪ͼ�滮���̨����ܹ�](docs/motion_navigation/architecture/known-map-background-planning-v1.md)
- [һ�����������ܹ�](docs/motion_navigation/architecture/jump-up-v1.md)
- [�����˶�����](docs/motion_navigation/architecture/block-motion-traits-v1.md)
- [Ŀ��״̬](docs/motion_navigation/architecture/goal-state-v1.md)
- [����ת��](docs/motion_navigation/architecture/movement-transitions-v1.md)
- [�����ƶ�ģʽ](docs/motion_navigation/architecture/ground-modes-v1.md)
- [֧������С̨��](docs/motion_navigation/architecture/support-surfaces-v1.md)
- [����ת��](docs/motion_navigation/architecture/air-transitions-v1.md)
- [�˶�������](docs/motion_navigation/architecture/physics-calculator-1_21-v1.md)
- [B10 �������������ִ��](docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md)
- [C1 ս������Ƭ](docs/motion_navigation/architecture/C1-combat-vertical-slice-v1.md)
- [C1-B �ƶ�Ŀ��������ս](docs/motion_navigation/architecture/C1B-moving-target-melee-v1.md)
- [C1-C �ⲿ�˶����ܻ��ָ�](docs/motion_navigation/architecture/C1C-external-motion-recovery-v1.md)
- [C1-C ʹ���ܿص�ԭ�湥�������ܻ�](docs/motion_navigation/decisions/0018-controlled-disturbance-for-c1c-acceptance.md)
- [B10 ������ս������Ƭ](docs/motion_navigation/decisions/0013-combat-vertical-slice-after-b10.md)
- [B10 �������](docs/motion_navigation/decisions/0014-b10-audit-hardening.md)
- [��ս�����빥�������߽�](docs/motion_navigation/decisions/0015-engagement-awareness-and-attack-capability.md)
- [C1-B Ŀ����ʵ�������͹̶�����](docs/motion_navigation/decisions/0016-c1b-target-vitality-and-seeds.md)
- [ͳһ�����ⲿ�˶�](docs/motion_navigation/decisions/0017-unified-external-motion-recovery.md)
- [�˶���������߽�](docs/motion_navigation/architecture/package-boundaries-v1.md)
- [����˳�����](docs/motion_navigation/decisions/0001-incremental-delivery-order.md)
- [������վ���](docs/motion_navigation/decisions/0002-three-reference-versions.md)
- [B02 Fabric ���շ�Χ](docs/motion_navigation/decisions/0005-b02-fabric-acceptance-scope.md)
- [���� Fabric ʵ����Χ](docs/motion_navigation/decisions/0006-fabric-only-live-development.md)
- [B03 �������վ���](docs/motion_navigation/decisions/0007-b03-absolute-acceptance.md)
- [B04 ��֪ͼ�װ����](docs/motion_navigation/decisions/0008-b04-known-graph-first.md)
- [�������֪�����ƶ�](docs/motion_navigation/decisions/0009-known-world-mobility-before-exploration.md)
- [B08 Crawl ��֤��ȡ��](docs/motion_navigation/decisions/0010-b08-evidence-and-observed-crawl.md)
- [�����˶�������](docs/motion_navigation/decisions/0011-b09r-calculator-before-continuous-handoff.md)
- [B10 �ֲ����붯�����](docs/motion_navigation/decisions/0012-b10-staged-motion-solving.md)

������ճе���ͬ�ıȽ���;��û���κ�һ�汻ָ��Ϊ�¼ܹ���������ǰ�ѽ��� Walk��Sprint��Crouch���Ϸ�Ԥ�ú�� Crawl��һ���϶������һ�����˺��½��������� B05 ��һ��������B09-R ֻ�����������ĺ����B10 �ѽ���״̬ê�㡢��ʵ����Ӧ���˱�������ͶӰ����������⡢�滮���ɺ�����ִ�С�`JumpGap` Ĭ���ߺ�̨������ִִ�������������� Crawl����������Ӿ�����������δ��Ȩ��

## 4. ����Ҫ��

### ��Ҫ���ȹ��̻�

- ֻʵ�ֵ�ǰ�׶�ʵ����Ҫ����С�ջ�������ǰ���������ܹ��Ŀտǡ�
- û��ʵ��ƿ��ʱ������������̡߳����̡����桢��ܡ����ϵͳ��ͨ�ó���㡣
- ��Ϊ�����������ӿ��ء����ݷ�֧��������³�������Ҫ�����ǰ������ȷ�ظ��㣬���γ������״̬�����Ͳ��Ա߽硣
- ���ȸ����Ѿ���֤����Ϸ���롢�۲⡢����ִ�к�֤�ݹ��ߣ�����ǰ��Ҫͨ���½ӿڵ����ա�
- ����Ӧ��֤��Ϊ���ӿڻ�ʧ�ܱ߽磬����дֻ����ʵ�ֵĵͼ�ֵ���ԡ�

### ���ֿ�ά��

- ÿ�೤��״ֻ̬��һ��ӵ���ߣ�����ģ��ͨ����ȷ�ӿڶ�ȡ�����¼���
- ����ֱ�ӱ�����;����Ҫ�� `reason` �ַ�����ɢ�䲼�����ػ�ȫ�ֱ������о���Ȩ�޺��������ڡ�
- ģ�鱣��С��������һ���޸�Χ��һ����������Ϊ����ͬʱ�ĸ�֪Ȩ�ޡ�·�����ֺ͵ײ���ơ�
- ������Լ���ʱ��ͬ����������ߡ������ߡ����Ժ������ĵ���
- ɾ����ʱ����ǰ��ȷ��û��������ں�֤������������ɾ���û����硢ģ�͡��켣�����㡢��ʾ���ݻ�ʧ�ܽ����

### ���ֿ���չ

- ����������Ҫ��չĿ�������������Ծ��Ҫ��չ�ƶ������Ͷ���ִ�У�����������Ҫ��չ֪ʶ�����κ������жϡ�
- ����Ѱ·�㷨Ӧ��Ҫ�滻��̨�滮�ڲ����Ż�ת��Ӧ��Ҫ�޸ľֲ����١�
- ģ����Ի����ṩ���ݣ�������Ϊһ������������Ŀ�ꡢ��ͼ��·�ߡ��������ɻ�����״̬��
- ���ȶ��ӿںͲ����������Ż��㷨�ڲ�����һ�ι��ܱ����޸Ĵ����ģ�飬�ȼ��ְ���Ƿ�Ŵ�λ�á�

## 5. ��Ŀ���߽�

- ��ʽ������ʹ�úϷ��Ľṹ���۲졣δ֪�ռ䲻�ܵ�������������ȫ֪��Ϣ����ͨ������ `TEST_ORACLE` ��ڣ����ܽ�����ʽ actor��
- ��ʽ����Ĭ����ͷ����ͼ��`pov_debug` ֻ�����˹���ȷҪ�����ϣ����ı���ʽ��Ϊ���塣
- �����׶ε���ʽʵ����ʵ������ֻʹ�ö��� Fabric�������۲졢�������˶������Ա��ֺ���޹أ����� CraftGround ���뱣�����������ɽ׶��ż��������µ���ƾ�����ȷ�ָ���
- ֻ�ж����ٲú��Ψһ������ڿ���д��ҿ��ơ��������������ڳɹ��������ɺ����۲�ȷ�ϡ�
- CraftGround �Ͷ��� Fabric ���ʹ��ͬһ�׹۲��붯�����塣����ֱ���޸����硢����������ð�������Ϊ��
- ���������ⲿ������ʵ�飬����Ĭ�������������ָ��޹� stash�����������˹������޸ġ�
- ��ȫ��Ȩ�޺�ʧ�ܷ��಻�ܱ�·�����������δʵ�֡�ȱ����Ϣ������δ��ɡ���ȷ�����ͳ�ʱ����ֱ���
- Ĭ��ʹ����Ȼ��ֱ�ӵ����ı�д�����ͼ�¼����˵���ۣ���˵��ԭ��֤�ݺͱ߽硣

## 6. �����뵱ǰ�������

- ��������`D:\My_project\mc_ai`
- ��Ŀ������`D:\My_project\mc_ai\.venv`
- Python ͨ�� `D:\Miniforge3\Scripts\conda.exe run` ���ã�������ϵͳ Python ��ȫ�� PATH��
- �汾����Ϊ Python 3.11��OpenJDK 21��Minecraft 1.21��CraftGround 2.7.4 / runtime 0.1.0��Fabric Loader 0.15.11��Fabric API 0.100.6+1.21�����þ�Ĭ������

�ڲֿ��Ŀ¼�����˶�������ר���飺

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

������湫�����ա��汾ע����ͳ���ע�����

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/motion_navigation_references.py verify --snapshot-root output/github-navigation-code-share-v2
```

����һ�����������ķֶ�֤�ݶ�ȡ��

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_segmented_trace -v
```

�ύǰ���� `git diff --check`����ȷ���ݴ���ֻ�������������ļ��������׶������µ���ʽ���ʱ������������д���Ӧ `acceptance` �ĵ�������ֻ������ǰ�����Ҫ��������ۻ���ʷ���

## 7. ������ʽ

- ��ʼ�޸�ǰ������ `git status --short --branch`�������뵱ǰ�����޹ص��޸ġ�
- �ȸ��������д��ʧ�ܼ�飬��ʵʩ��С��������ɺ�����ֱ����ؼ�顣
- �ƻ�������ʵ�֣�������Բ�����ʵ��ͨ�������޳����ɹ������������Ѿ��ձ�ɿ���
- ֻ�ύ��ǰ�����ļ��������û���ȷҪ�󣬲����͡���������������ѵ�����ⲿ������ʵ�顣
- �Ǳ�Ҫ��ʹ���� agent��ʹ��ʱ���뻮���ļ�����Ȩ�������� agent ͬʱ�޸Ĺ����ļ���
