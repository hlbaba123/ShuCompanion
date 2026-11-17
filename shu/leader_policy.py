"""Bounded goal semantics, independent of GUI permission and completion proof.

The model classifies intent. This module checks literal target/count/resource
facts and evaluates what an import receipt can cover; it never routes a GUI
action, grants authority, or treats model prose as external evidence.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re


KINDS = ('lookup', 'import', 'repeat_stage', 'clear_event', 'base', 'unknown')
_FIELDS = frozenset({'kind','stage_code','repeat_count','medicine_limit',
                     'premium_limit','stop_after_current','unresolved'})
_CODE = re.compile(r'(?<![A-Za-z0-9_-])(?:[A-Za-z][A-Za-z0-9]{0,15}|[0-9]{1,3})(?:-[A-Za-z0-9]{1,12}){1,3}(?![A-Za-z0-9_-])')
_NUMBER = r'(?:[0-9]{1,4}|[零〇一二两三四五六七八九十百千]{1,6})'
_COUNT = re.compile(r'(?:刷|打|完成|通关|执行|重复|运行|改成|改为|连打|次数(?:改成|改为|设为)?)[^，。；\n]{0,40}?('+_NUMBER+r')\s*(?:次|局|轮|遍)(?!\s*(?:理智药|药剂|源石))')
_COUNT_END = re.compile('('+_NUMBER+r')\s*(?:次|局|轮)(?:真实)?(?:结算|后停|后停止)')
_NO_MED = re.compile(r'(?:不(?:使用|用|吃)|不要(?:使用|用|吃))\s*(?:任何)?(?:理智药|药剂|药)')
_NO_PREMIUM = re.compile(r'(?:不(?:使用|用|花|吃)|不要(?:使用|用|花|吃))\s*(?:任何)?(?:源石|至纯源石)|不碎石')
_AFTER = re.compile(r'(?:这|本|当前)(?:一)?(?:局|轮|次).*?(?:结束|完成|打完).*?停|(?:完成|打完)当前(?:局|轮).*?停')


class TaskSpecError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    kind: str = 'unknown'
    stage_code: str | None = None
    repeat_count: int | None = None
    medicine_limit: int | None = None
    premium_limit: int | None = None
    stop_after_current: bool = False
    unresolved: tuple[str, ...] = ()

    def as_dict(self):
        return {'kind':self.kind,'stage_code':self.stage_code,'repeat_count':self.repeat_count,
                'medicine_limit':self.medicine_limit,'premium_limit':self.premium_limit,
                'stop_after_current':self.stop_after_current,'unresolved':list(self.unresolved)}


def _number(text):
    if text.isdigit(): return int(text)
    digits = {'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    total = current = 0
    for char in text:
        if char in digits: current = digits[char]
        elif char in '十百千':
            total += (current or 1) * {'十':10,'百':100,'千':1000}[char]
            current = 0
        else: raise TaskSpecError('次数不是可识别的整数。')
    return total + current


def _count(text):
    values = [_number(m.group(1)) for pattern in (_COUNT,_COUNT_END) for m in pattern.finditer(text)]
    return values[-1] if values else None


def _resource_limit(text, resource):
    negative = _NO_MED if resource == 'medicine' else _NO_PREMIUM
    if negative.search(text): return 0
    name = r'(?:理智药|药剂|药)' if resource == 'medicine' else r'(?:至纯)?源石'
    pattern = re.compile(r'(?:最多|允许|可以|可|使用|用|吃|花)\s*(?:使用|用|吃|花)?\s*('+_NUMBER+r')\s*(?:瓶|个|颗|次)?\s*'+name)
    matches = list(pattern.finditer(text))
    return _number(matches[-1].group(1)) if matches else None


def _battle_requested(text):
    # A declared no-start boundary is not a positive start request.
    text = re.sub(r'(?:不得|不要|不能|不)\s*(?:声称|宣称|表示|报告)(?:已|已经)?'
                  r'(?:(?!而是|而应|但|然后|随后|实际|接着)[^，,。；;\n])*', '', text)
    positive = re.sub(
        r'(?:绝不|不要|禁止|尚不|暂不|无需|尚未|并未|未曾|未|没有|不(?:得|能|要|再|需(?:要)?)?)'
        r'\s*(?:(?:发生|出现|点击|进行|执行|触发|发起)\s*)?(?:任何\s*)?'
        r'(?:(?:开始|启动)?(?:自动)?(?:作战|战斗)|开始|启动|开战)', '', text)
    # MAA's tab title and guide descriptions are nouns, not permission to run.
    positive = re.sub(r'[“"「\[（(]?自动(?:战斗|作战)[”"」\]）)]?\s*(?:页面|页签|标签页|栏目|界面|功能)', 'MAA作业页',positive)
    positive = re.sub(r'(?:通关|作战|战斗)(?:攻略|作业|方案|记录|录像|视频|要求)|已(?:经)?通关(?:的)?','资料',positive)
    # Both “普通关” and “普通关卡” contain “通关” across a word boundary.
    # Replacing this category noun leaves an independently requested
    # “通关普通关” intact, so battle objectives still cannot be downgraded.
    positive = positive.replace('普通关','一般关')
    return bool(_count(positive) is not None or re.search(r'开始(?:自动)?(?:作战|战斗)|开战|自动战斗|自动作战|刷(?:关|活动)|清(?:完|理)?(?:当前|新)?活动|通关',positive))


def _legacy_kind(text, repeat_count):
    if repeat_count is not None: return 'repeat_stage'
    if _battle_requested(text):
        return 'clear_event' if '活动' in text and not _CODE.search(text) else 'repeat_stage'
    if '基建' in text: return 'base'
    if _import_requested(text): return 'import'
    return 'unknown'


def _import_requested(text):
    positive=re.sub(r'(?:先不|尚不|暂不|不(?:要)?|禁止|无需)\s*(?:导入|加载(?:作业)?)', '', text)
    return bool(re.search(r'导入|加载作业',positive))


def _validated(value):
    if isinstance(value, TaskSpec): value = value.as_dict()
    if value is None: return {}
    if not isinstance(value,dict) or set(value)-_FIELDS:
        raise TaskSpecError('任务规范包含未知字段，不能增加执行权限。')
    result=dict(value)
    if 'kind' in result and (not isinstance(result['kind'],str) or result['kind'] not in KINDS):
        raise TaskSpecError('任务类别无效。')
    stage=result.get('stage_code')
    if stage is not None:
        if not isinstance(stage,str) or not _CODE.fullmatch(stage):
            raise TaskSpecError('关卡代码需为独立的真实代码，不能混入其他指令。')
        result['stage_code']=stage.upper()
    for field in ('repeat_count','medicine_limit','premium_limit'):
        item=result.get(field)
        if item is not None and (type(item) is not int or not (1 if field=='repeat_count' else 0)<=item<=9999):
            raise TaskSpecError('次数与资源额度需要是有界整数，未知用 null。')
    if 'stop_after_current' in result and type(result['stop_after_current']) is not bool:
        raise TaskSpecError('本局后停止标记需要是布尔值。')
    unresolved=result.get('unresolved',())
    if not isinstance(unresolved,(list,tuple)) or len(unresolved)>16:
        raise TaskSpecError('待核实事项需要是有界文本列表。')
    if any(not isinstance(x,str) or not x.strip() or len(x)>300 for x in unresolved):
        raise TaskSpecError('待核实事项无效。')
    if 'unresolved' in result: result['unresolved']=tuple(dict.fromkeys(x.strip() for x in unresolved))
    return result


def normalize_task_spec(value, *, user_request, goal, intent, previous=None,
                        constraints=(), inherit_previous=None):
    """Check model fields against explicit user literals and trusted prior facts.

    ``previous`` is a prior contract or TaskSpec. Only modifications, controls,
    and explicitly anaphoric execution inherit it; a new execution stays new.
    ``None`` resource limits remain unknown and never grant consumption.
    """
    if not isinstance(user_request,str) or not isinstance(goal,str):
        raise TaskSpecError('任务原话和目标需要是文本。')
    raw=_validated(value)
    inherit=(intent in {'modify','resume','pause','stop','status'} or intent=='execute' and not goal.strip()) if inherit_previous is None else bool(inherit_previous)
    prior={}
    if inherit and previous is not None:
        if isinstance(previous,TaskSpec): prior=previous.as_dict()
        elif isinstance(previous,dict):
            if 'goal' in previous or 'task_spec' in previous:
                prior_value=previous.get('task_spec')
                if prior_value is None:
                    prior_value=normalize_task_spec(None,user_request='',goal=str(previous.get('goal','')),
                                                   intent='execute').as_dict()
                prior=_validated(prior_value)
            else: prior=_validated(previous)
        else: raise TaskSpecError('先前任务规范无效。')
    result={**TaskSpec().as_dict(),**prior}
    # Empty optional fields on a control/modification do not erase prior facts.
    for field,item in raw.items():
        if inherit and (item is None or field=='kind' and item=='unknown'): continue
        result[field]=item

    explicit_codes=list(dict.fromkeys(m.group().upper() for m in _CODE.finditer(user_request)))
    goal_codes=list(dict.fromkeys(m.group().upper() for m in _CODE.finditer(goal)))
    explicit_stage=explicit_codes[0] if len(explicit_codes)==1 else None
    trusted_stage=explicit_stage or (prior.get('stage_code') if inherit else None)
    if trusted_stage:
        if raw.get('stage_code') and raw['stage_code']!=trusted_stage:
            raise TaskSpecError('关卡代码与用户原话或当前任务不一致，不能把次数当成关卡。')
        if goal_codes and trusted_stage not in goal_codes:
            raise TaskSpecError('完整目标丢失或替换了已明确的关卡代码。')
        result['stage_code']=trusted_stage
    elif len(explicit_codes)>1:
        if raw.get('stage_code') and raw['stage_code'] not in explicit_codes:
            raise TaskSpecError('任务规范增加了用户未要求的关卡代码。')
    elif not result.get('stage_code') and len(goal_codes)==1:
        result['stage_code']=goal_codes[0]

    explicit_repeat=_count(user_request)
    goal_repeat=_count(goal)
    repeat=explicit_repeat if explicit_repeat is not None else (prior.get('repeat_count') if inherit else goal_repeat)
    if repeat is not None:
        if raw.get('repeat_count') is not None and raw['repeat_count']!=repeat:
            raise TaskSpecError('作战次数与用户原话或当前任务不一致。')
        if explicit_repeat is not None and goal_repeat is not None and goal_repeat!=explicit_repeat:
            raise TaskSpecError('完整目标中的次数与用户本次要求不一致。')
        result['repeat_count']=repeat

    for field,resource in (('medicine_limit','medicine'),('premium_limit','premium')):
        explicit_limit=_resource_limit(user_request,resource)
        if explicit_limit is not None:
            if raw.get(field) is not None and raw[field]!=explicit_limit:
                raise TaskSpecError('资源额度与用户明确限制不一致。')
            result[field]=explicit_limit
        elif inherit and prior.get(field) is not None:
            if raw.get(field) is not None and raw[field]!=prior[field]:
                raise TaskSpecError('未见用户明确修改，不能改变已有资源限制。')
            result[field]=prior[field]
        elif raw.get(field) is not None and raw[field]>0:
            raise TaskSpecError('正资源额度必须有用户本次明确原话依据。')
        else:
            # Conservative zero constraints are retained; positive model-created
            # constraint prose is not an authorization source.
            if any(_resource_limit(str(item),resource)==0 for item in constraints): result[field]=0

    if _AFTER.search(user_request): result['stop_after_current']=True
    inferred=_legacy_kind(user_request+'\n'+goal,result.get('repeat_count'))
    if raw.get('kind') in ('lookup','import') and inferred in ('repeat_stage','clear_event'):
        raise TaskSpecError('不能把用户要求的作战目标缩减成查询或导入。')
    if result['kind']=='unknown' or inferred in ('repeat_stage','clear_event'):
        result['kind']=inferred
    result['unresolved']=tuple(result.get('unresolved') or ())
    return TaskSpec(**result)


def _contract_dict(contract):
    return contract.as_dict() if hasattr(contract,'as_dict') else contract if isinstance(contract,dict) else {}


def import_satisfies_goal(contract):
    """Whether the *requested objective* can end at verified import.

    Legacy bare stage/import contracts retain their existing semantics. A
    workflow's implementation_scope/allow_start never narrows the user goal.
    """
    contract=_contract_dict(contract)
    text='\n'.join(str(contract.get(k) or '') for k in ('goal','user_request'))
    text+='\n'+'\n'.join(x for x in contract.get('completion_criteria',[]) if isinstance(x,str))
    if _battle_requested(text): return False
    spec=contract.get('task_spec')
    kind=spec.get('kind') if isinstance(spec,dict) else getattr(spec,'kind',None)
    return kind in (None,'unknown','import') and '基建' not in text


def copilot_research_required(contract):
    """Recognize requested copilot deliverables, without choosing any answer."""
    contract=_contract_dict(contract)
    if contract.get('current_deliverable') in {'stage_catalog', 'chat'}:
        return False
    if contract.get('current_deliverable') in {'copilot_codes', 'copilot_json'}:
        return True
    text='\n'.join(str(contract.get(k) or '') for k in ('goal','user_request'))
    spec=contract.get('task_spec') or {}
    return bool((spec.get('stage_code') and re.search(r'作业|copilot',text,re.I))
                or re.search(r'神秘代码|作业码|完整\s*JSON|下载.{0,12}作业',text,re.I)
                or re.search(r'(?:查询|查找|寻找|搜索|获取|找|列出).{0,50}作业(?!格式|规范|协议|资料|文档|含义|是什么|怎么)',text))


def research_stage_codes(contract, receipt=None):
    """Preserve every explicit code/range before considering a model scope.

    A singular task_spec is a hint, never authority to omit other codes in the
    user's original request. Ranges and extra listed stages may be combined.
    """
    contract=_contract_dict(contract)
    if contract.get('current_deliverable') in {'stage_catalog', 'copilot_codes', 'maa_import'}:
        explicit = contract.get('target_stage_codes') or []
        if not isinstance(explicit, list) or len(explicit) > 200:
            raise TaskSpecError('结构化关卡范围无效。')
        spec = contract.get('task_spec') or {}
        codes = [*explicit]
        if not codes and spec.get('stage_code'):
            codes.append(spec['stage_code'])
        if any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in codes):
            raise TaskSpecError('结构化关卡代码无效。')
        if (contract.get('current_deliverable') == 'maa_import'
                and 'target_stage_codes' not in contract and not codes):
            # Legacy imports have no typed list. Parse the original request,
            # keeping explanatory model group names out of target selection.
            contract = {**contract, 'goal':''}
        else:
            return list(dict.fromkeys(code.upper() for code in codes))
    pattern=re.compile(r'([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z]+)*)-(\d+)\s*(?:至|到|~|～|→|->|—)\s*(?:\1-)?(\d+)',re.I)
    codes=[]
    for text in (str(contract.get('goal') or ''),str(contract.get('user_request') or '')):
        ranges=list(pattern.finditer(text));tokens=[]
        for match in ranges:
            prefix,start,end=match.groups();start,end=int(start),int(end)
            if not 0<start<=end<start+200:
                raise TaskSpecError('明确关卡范围无效或超过200关，不能按部分关卡完成。')
            tokens.append((match.start(),[f'{prefix.upper()}-{i}' for i in range(start,end+1)]))
        for match in _CODE.finditer(text):
            if not any(span.start()<=match.start()<span.end() for span in ranges):
                tokens.append((match.start(),[match.group().upper()]))
        for _,values in sorted(tokens):codes.extend(values)
    spec=contract.get('task_spec') or {}
    if spec.get('stage_code'):codes.append(str(spec['stage_code']).upper())
    if codes:return list(dict.fromkeys(codes))
    scope=(receipt or {}).get('activity_scope') or {}
    return scope.get('required_stage_codes') or []


def activity_scope_complete(scope, catalog_codes):
    """A registered activity scope accounts for every directory stage exactly."""
    if not isinstance(scope,dict) or scope.get('coverage_verified') is not True:return False
    required=scope.get('required_stage_codes');excluded=scope.get('excluded_stages')
    if (not isinstance(required,list) or not required or not isinstance(excluded,list)
            or any(not isinstance(code,str) or not code for code in required)
            or len(set(required))!=len(required)):return False
    omitted=[]
    for row in excluded:
        if (not isinstance(row,dict) or not isinstance(row.get('code'),str)
                or not isinstance(row.get('reason'),str) or not row['reason'].strip()
                or not isinstance(row.get('source_url'),str) or not row['source_url']
                or not isinstance(row.get('source_ref'),dict)):
            return False
        if row.get('reason_kind')=='not_ordinary':
            if (activity_stage_kind(row['code'])=='ordinary' or row.get('source_quote')!=row['code']
                    or row.get('stage_kind')!=activity_stage_kind(row['code'])
                    or row.get('source_ref')!=scope.get('catalog_ref')
                    or row.get('source_url')!='https://prts.maa.plus/arknights/level'):return False
        elif row.get('reason_kind')=='not_open_yet':
            if not isinstance(row.get('source_quote'),str) or not row['source_quote'] or not row.get('opens_at'):return False
        else:return False
        omitted.append(row['code'])
    all_codes=set(catalog_codes)
    return (bool(all_codes) and len(set(omitted))==len(omitted) and not set(required)&set(omitted)
            and set(required)|set(omitted)==all_codes
            and set(scope.get('catalog_stage_codes') or [])==all_codes)


def activity_stage_kind(code):
    """Classify an exact public code, never nearby story/order metadata."""
    pieces=str(code).upper().split('-')
    if len(pieces)>1 and pieces[-1].isdigit():
        for label,kind in (('TR','tutorial'),('ST','story'),('EX','ex'),('S','special')):
            if label in pieces[:-1]:return kind
    return 'ordinary'


def opening_exclusion_supported(item, proof, *, now=None):
    """Require a real short quotation naming this stage/category and future date.

    An announcement URL, story node position or chapter heading alone cannot
    establish that a normal stage is closed. A category quote must explicitly
    refer to normal stages; the broad term 活动关卡 does not identify that scope.
    """
    from datetime import datetime,timezone,timedelta
    from .network_dialogue import _sources
    if not isinstance(item,dict) or not isinstance(proof,dict):return False
    quote=item.get('source_quote');url=item.get('source_url');opens=item.get('opens_at')
    if (not isinstance(quote,str) or not 1<=len(quote)<=240 or not isinstance(url,str)
            or not isinstance(opens,str) or not opens.endswith('+08:00')):return False
    try:
        shanghai=timezone(timedelta(hours=8));opens=datetime.fromisoformat(opens).astimezone(shanghai)
        current=(now or datetime.now(shanghai)).astimezone(shanghai)
    except (ValueError,TypeError):return False
    if opens<=current:return False
    tool=proof.get('tool');result=proof.get('result') or {}
    if tool not in {'research_search','research_fetch'} or result.get('ok') is not True:return False
    kind='web_fetch' if tool=='research_fetch' else 'web_search'
    if url not in {source['url'] for source in _sources(result,kind)}:return False
    texts=[]
    def collect(value, active=False, depth=0):
        if depth>12 or len(texts)>300:return
        if isinstance(value,str):
            if active:texts.append(value)
        elif isinstance(value,list):
            for child in value:collect(child,active,depth+1)
        elif isinstance(value,dict):
            own_url=next((value[k] for k in ('url','link','source_url','webpage_url') if isinstance(value.get(k),str)),None)
            if own_url is not None:active=own_url==url
            for key,child in value.items():
                if isinstance(child,(dict,list)):collect(child,active,depth+1)
                elif key in {'content','text','body','snippet','summary','description','title'}:collect(child,active,depth+1)
    collect(result.get('data'),tool=='research_fetch' and result.get('url')==url)
    if not any(quote in text for text in texts):return False
    # Bind the selected date and target to the same short source clause. A
    # later EX opening line cannot be attached to earlier normal-stage text.
    for clause in re.split(r'[\r\n。；;]',quote):
        try:explicit=research_stage_codes({'goal':clause})
        except TaskSpecError:continue
        code=str(item.get('code') or '').upper()
        target=(code in explicit or activity_stage_kind(code)=='ordinary'
                and bool(re.search(r'普通(?:作战)?关卡|常规(?:作战)?关卡|普通作战|常规作战',clause)))
        if not target or not re.search(r'开放|开启|解锁|\bopens?\b|\bavailable\b',clause,re.I):continue
        years={int(year) for year in re.findall(r'(?<!\d)(20\d{2})(?=年|[-/.])',clause)}
        if years and opens.year not in years or not years and opens.year!=current.year:continue
        date_found=bool(re.search(rf'(?<!\d)0?{opens.month}\s*月\s*0?{opens.day}\s*日',clause)
            or re.search(rf'{opens.year}[-/.]0?{opens.month}[-/.]0?{opens.day}(?!\d)',clause))
        if not date_found:continue
        times={(int(hour),int(minute)) for hour,minute in re.findall(r'(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)',clause)}
        if times and (opens.hour,opens.minute) not in times:continue
        if not times and opens.date()==current.date():continue
        return True
    return False


def complete_pool_codes(pools):
    """Codes whose individually bound public candidate pool is complete."""
    result=set()
    for pool in pools or []:
        if (not isinstance(pool,dict) or not isinstance(pool.get('coverage'),dict)
                or pool['coverage'].get('complete') is not True or not pool.get('pool_ref')):continue
        for stage in (pool.get('stage_resolution') or {}).get('levels',[]):
            if isinstance(stage,dict) and stage.get('code') and stage.get('stage_id'):
                result.add(str(stage['code']).upper())
    return result


def activity_job_goal(contract):
    contract=_contract_dict(contract)
    text='\n'.join(str(contract.get(k) or '') for k in ('goal','user_request'))
    return copilot_research_required(contract) and not (contract.get('task_spec') or {}).get('stage_code') and bool(re.search(r'活动|SideStory|故事集|复刻',text,re.I))


def roster_required(contract):
    # User's product policy: normal game-job discovery is account-aware. Pure
    # public-format/docs lookup or an explicit read-only testing exemption is not.
    contract=_contract_dict(contract);text='\n'.join(str(contract.get(k) or '') for k in ('goal','user_request'))
    if re.search(r'不(?:做|进行)?(?:筛选|匹配|适配)(?:干员|阵容)?|本轮不筛选',text):return False
    return copilot_research_required(contract) and (contract.get('match_roster') is True or bool(re.search(r'适合|适配|筛选|匹配|我的干员|我的阵容',text)))


def research_satisfies_goal(contract, receipt):
    """Only an explicitly read-only goal may finish at tool-backed research."""
    contract=_contract_dict(contract)
    spec=contract.get('task_spec') or {}
    text='\n'.join(str(contract.get(k) or '') for k in ('goal','user_request'))
    # Initial unknowns are planning facts, not newly authorized deliverables.
    # Leader still reviews the user's objective. Exact copilot proof and actual
    # requested account matching below remain required; input gates are separate.
    if (not isinstance(spec,dict) or spec.get('kind')!='lookup'
            or _battle_requested(text) or _import_requested(text) or '基建' in text):
        return False
    if not (isinstance(receipt,dict) and receipt.get('status') in ('researched','observed','completed')
            and receipt.get('evidence_refs')):
        return False
    if copilot_research_required(contract):
        # Generated by the role adapter after a full download and exact public
        # stage lookup. The durable completion boundary rechecks the bytes.
        candidates=receipt.get('verified_copilots') or []
        expected=spec.get('stage_code')
        if not receipt.get('candidate_refs') or not any(isinstance(c,dict)
                and (not expected or str(c.get('code','')).casefold()==str(expected).casefold())
                and re.fullmatch(r'[1-9][0-9]{0,11}',str(c.get('copilot_id','')))
                and c.get('mystery_code')=='prts://'+str(c.get('copilot_id')) for c in candidates):
            return False
        codes=research_stage_codes(contract,receipt)
        if activity_job_goal(contract):
            catalog=receipt.get('activity_catalog') or {}
            scope=receipt.get('activity_scope') or {}
            if (not codes or catalog.get('ok') is not True or catalog.get('has_more') is True
                    or scope.get('event_name')!=catalog.get('event_name')
                    or scope.get('catalog_ref')!=catalog.get('stage_catalog_ref')
                    or not activity_scope_complete(scope,[s.get('code') for s in catalog.get('stages',[])
                        if isinstance(s,dict) and s.get('code')])):return False
        verified_codes={c.get('code') for c in candidates if isinstance(c,dict)}
        if any(code not in verified_codes for code in codes):return False
        if re.search(r'(?:全部|所有|全量).{0,20}(?:作业|候选|摘要)|(?:作业|候选|摘要).{0,12}(?:全部|所有|全量)',text):
            pools=receipt.get('candidate_pools') or []
            if codes:
                if set(codes)-complete_pool_codes(pools):return False
            elif not any(isinstance(p,dict) and isinstance(p.get('coverage'),dict)
                         and p['coverage'].get('complete') is True and p.get('pool_ref') for p in pools):return False
    positive_text=re.sub(r'(?:本轮|目前|暂时|先)?(?:不|无需|不要|暂不)(?:做|进行)?(?:筛选|匹配|适配)(?:干员|阵容|作业)?','',text)
    positive_text=re.sub(r'(?:供|用于|以便)(?:后续|以后|下阶段)(?:的)?(?:筛选|匹配|适配)','',positive_text)
    if roster_required(contract) or re.search(r'适合|适配|筛选|匹配|我的干员|我的阵容',positive_text):
        # Public guide requirements alone cannot prove account compatibility.
        assessments=receipt.get('assessments') or {}
        good=[c for c in receipt.get('verified_copilots',[]) if
              isinstance(assessments.get(c.get('copilot_id')),dict) and assessments[c['copilot_id']].get('classification')=='satisfied']
        codes=research_stage_codes(contract,receipt)
        if not good or any(code not in {c.get('code') for c in good} for code in codes):
            return False
    return True


def research_goal_diagnostics(contract, receipt):
    """Explain bounded structural gaps; never replace persistent proof checks.

    Only explicit goal fields and adapter-produced receipt fields participate.
    Model summary prose cannot provide a scope, download, pool or roster match.
    Paths/hashes still need the final completion boundary's file, dispatch,
    epoch and current-roster verification even when ``satisfied`` is true.
    """
    contract=_contract_dict(contract)
    receipt=receipt if isinstance(receipt,dict) else {}
    issues=[];truncated=False

    def issue(code,message):
        if len(issues)<16:issues.append({'code':code,'message':message})

    def rows(value,limit=400):
        nonlocal truncated
        if not isinstance(value,(list,tuple)):return []
        if len(value)>limit:truncated=True
        return list(value[:limit])

    def codes(value):
        nonlocal truncated
        result=[]
        for code in rows(value,200):
            if isinstance(code,str) and _CODE.fullmatch(code):
                code=code.upper()
                if code not in result:result.append(code)
        return result

    # These bounds exceed the normal validated contract limits. Do not parse a
    # truncated goal and silently accept the surviving fraction as its scope.
    for key in ('goal','user_request'):
        if isinstance(contract.get(key),str) and len(contract[key])>32000:
            truncated=True
    safe_contract=contract if not truncated else {
        key:value for key,value in contract.items() if key not in ('goal','user_request')}
    text='\n'.join(str(safe_contract.get(k) or '') for k in ('goal','user_request'))
    spec=safe_contract.get('task_spec') or {}
    if not isinstance(spec,dict):spec={}
    # Guard helpers against malformed optional containers without copying or
    # editing any authoritative receipt in place.
    scope=receipt.get('activity_scope')
    scope=scope if isinstance(scope,dict) else {}
    try:required=codes(research_stage_codes(safe_contract,{'activity_scope':scope}))
    except (TaskSpecError,TypeError,ValueError,AttributeError):
        required=[]
        issue('invalid_stage_scope','明确关卡范围无效，需修正范围后再核验，不能按部分关卡结束。')

    readonly=(spec.get('kind')=='lookup' and not _battle_requested(text)
        and not _import_requested(text) and '基建' not in text)
    copilot=copilot_research_required(safe_contract)
    activity=activity_job_goal(safe_contract) if isinstance(safe_contract.get('task_spec',{}),dict) else False
    positive_text=re.sub(r'(?:本轮|目前|暂时|先)?(?:不|无需|不要|暂不)(?:做|进行)?(?:筛选|匹配|适配)(?:干员|阵容|作业)?','',text)
    positive_text=re.sub(r'(?:供|用于|以便)(?:后续|以后|下阶段)(?:的)?(?:筛选|匹配|适配)','',positive_text)
    matching=roster_required(safe_contract) or bool(re.search(r'适合|适配|筛选|匹配|我的干员|我的阵容',positive_text))
    evidence=rows(receipt.get('evidence_refs'))
    references=rows(receipt.get('candidate_refs'))
    valid=[]
    for row in rows(receipt.get('verified_copilots')):
        if not isinstance(row,dict):continue
        identifier=str(row.get('copilot_id',''));code=row.get('code')
        if (isinstance(code,str) and _CODE.fullmatch(code)
                and re.fullmatch(r'[1-9][0-9]{0,11}',identifier)
                and row.get('mystery_code')=='prts://'+identifier):
            valid.append((code.upper(),identifier))
    verified={code for code,_ in valid} if references else set()
    missing=[code for code in required if code not in verified] if copilot else []
    missing_candidates=copilot and (not references or not valid)
    missing_evidence=not evidence

    catalog=receipt.get('activity_catalog')
    catalog=catalog if isinstance(catalog,dict) else {}
    catalog_codes=codes([row.get('code') for row in rows(catalog.get('stages'),200) if isinstance(row,dict)])
    missing_catalog=activity and (catalog.get('ok') is not True or catalog.get('has_more') is True
        or not catalog_codes or not isinstance(catalog.get('stage_catalog_ref'),dict))
    missing_scope=False
    if activity:
        try:
            missing_scope=(missing_catalog or scope.get('event_name')!=catalog.get('event_name')
                or scope.get('catalog_ref')!=catalog.get('stage_catalog_ref')
                or not activity_scope_complete(scope,catalog_codes))
        except (TypeError,ValueError,KeyError,AttributeError):missing_scope=True

    all_summaries=copilot and bool(re.search(
        r'(?:全部|所有|全量).{0,20}(?:作业|候选|摘要)|(?:作业|候选|摘要).{0,12}(?:全部|所有|全量)',text))
    pools=[]
    for row in rows(receipt.get('candidate_pools')):
        if not isinstance(row,dict):continue
        resolution=row.get('stage_resolution')
        if not isinstance(resolution,dict):resolution={}
        pools.append({**row,'stage_resolution':{**resolution,'levels':rows(resolution.get('levels'),200)}})
    complete=complete_pool_codes(pools)
    incomplete=[code for code in required if code not in complete] if all_summaries else []
    missing_pool=all_summaries and (bool(incomplete) or not required and not any(
        isinstance(p.get('coverage'),dict) and p['coverage'].get('complete') is True and p.get('pool_ref') for p in pools))
    assessments=receipt.get('assessments')
    assessments=assessments if isinstance(assessments,dict) else {}
    good={code for code,identifier in valid if isinstance(assessments.get(identifier),dict)
        and assessments[identifier].get('classification')=='satisfied'} if references else set()
    unmatched=[code for code in required if code not in good] if matching else []
    missing_match=matching and (bool(unmatched) or not good)

    if not readonly:issue('not_readonly_goal','只读资料不能完成当前导入或作战目标；保留用户完整要求。')
    if receipt.get('status') not in {'researched','observed','completed'}:
        issue('research_not_returned','研究尚未以有效状态返回完整结果。')
    if missing_evidence:issue('missing_evidence','尚缺实际工具证据；角色回复不算资料来源。')
    if missing_candidates:issue('missing_candidates','尚缺登记的完整JSON下载凭据和对应真实神秘代码。')
    if missing_catalog:issue('missing_activity_catalog','活动目标尚缺完整真实关卡目录；先核实目录，不猜关卡。')
    if missing_scope:issue('missing_activity_scope','活动目标尚未登记完整关卡范围及逐关排除依据；已有作业可复用，补齐范围凭据即可。')
    if missing:issue('missing_stage_codes','部分要求关卡尚缺真实代码及完整JSON；只补未覆盖关卡，不重查已有结果。')
    if missing_pool:issue('incomplete_candidate_pools','全部摘要目标尚缺逐关完整候选池；继续已有池分页，不重新建立查询。')
    if missing_match:issue('unmatched_stage_codes','部分要求关卡尚无满足本地干员硬性条件的完整JSON评估；未核实借人不算满足。')
    if truncated:issue('diagnostic_input_limit','结构性输入超过诊断上限；这里只展示有界缺口，不能据此判定完整完成。')
    try:
        satisfied=not issues and research_satisfies_goal(safe_contract,receipt)
    except (TaskSpecError,TypeError,ValueError,KeyError,AttributeError):satisfied=False
    if not satisfied and not issues:
        issue('completion_structure_unverified','现有收据未覆盖完整目标结构，需核对明确关卡与产物对应。')
    return {'schema':1,'authority':'structural_only','satisfied':bool(satisfied),
        'readonly_goal':readonly,'copilot_required':copilot,'roster_required':matching,
        'required_stage_codes':required,'missing_evidence':missing_evidence,
        'missing_candidates':bool(missing_candidates),'missing_activity_catalog':bool(missing_catalog),
        'missing_activity_scope':bool(missing_scope),'missing_stage_codes':missing,
        'incomplete_pool_codes':incomplete,'unmatched_stage_codes':unmatched,
        'issues':issues,'truncated':truncated}


def verify_queue_completion(contract, plan, receipt):
    """Check total coverage after the caller has verified local evidence and ledger."""
    covered, missing, conflicts = [], [], []
    try:
        targets = [target['target_key'] for target in plan['targets']]
        encoded = json.dumps({key:value for key,value in plan.items() if key != 'plan_sha256'},
                             ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        if (plan.get('schema') != 'maa_queue_plan/v1' or not targets or len(set(targets)) != len(targets)
                or plan.get('plan_sha256') != digest or receipt.get('schema') != 'maa_queue_receipt/v1'
                or receipt.get('plan_sha256') != digest or receipt.get('battle_start_submitted') is not False
                or receipt.get('preserved_baseline') is not True or receipt.get('unexpected_rows') != []):
            raise ValueError('queue_scope_or_baseline_invalid')
        items = {item['target_key']:item for item in receipt['items']}
        if len(items) != len(receipt['items']) or set(items) - set(targets):
            raise ValueError('queue_duplicate_or_unexpected_target')
        rows = set()
        for key in targets:
            item = items.get(key,{})
            status = item.get('status')
            row = item.get('row_identity')
            added = status == 'added' and item.get('owned') is True and bool(item.get('operation_ids'))
            reused = status == 'reused' and item.get('owned') is False and item.get('operation_ids') == []
            if (added or reused) and isinstance(row,str) and row and row not in rows and item.get('evidence_refs'):
                covered.append(key)
                rows.add(row)
            else:
                missing.append(key)
                if status in {'added','reused','conflict','unknown'}:
                    conflicts.append({'target_key':key,'code':'queue_item_unverified'})
    except (AttributeError,KeyError,TypeError,ValueError):
        covered = []
        missing = list(dict.fromkeys(target['target_key'] for target in (plan or {}).get('targets', [])
            if isinstance(target,dict) and isinstance(target.get('target_key'),str))) if isinstance(plan,dict) else []
        conflicts = [{'code':'queue_coverage_invalid'}]
    complete = bool(covered) and not missing and not conflicts and import_satisfies_goal(contract)
    return {'status':'import_verified' if complete else 'needs_input','complete':complete,'goal_complete':complete,
            'code':'queue_goal_covered' if complete else 'queue_goal_incomplete',
            'summary':f'已核实 {len(covered)} 个队列目标。' if complete else f'已核实 {len(covered)} 个目标，完整任务仍待确认。',
            'covered_target_keys':covered,'missing_target_keys':missing,'conflicts':conflicts}


def completion_decision(contract, receipt):
    """Coverage decision; the caller still verifies actual GUI/hash receipts."""
    contract=_contract_dict(contract)
    valid=(isinstance(receipt,dict) and receipt.get('status')=='import_verified'
           and receipt.get('battle_start_submitted') is False and bool(receipt.get('evidence_refs')))
    satisfied=valid and import_satisfies_goal(contract)
    if satisfied:
        pending=[]; summary='用户要求的作业导入已有回读证据；未进行作战。'; code='import_goal_covered'
    elif valid:
        pending=[str(contract.get('goal') or '完成用户全部目标并核实结果')[:1600]]
        summary='作业导入已核实，但用户要求的后续目标尚未完成；当前实现不提供开战工具。'
        code='goal_not_covered_by_import'
    else:
        pending=[str(contract.get('goal') or '取得符合目标的真实执行证据')[:1600]]
        summary='尚缺可核实的执行结果，不能把角色回复当作任务完成。'; code='completion_evidence_missing'
    return {'status':'import_verified' if satisfied else 'needs_input',
            'goal_complete':satisfied,'complete':satisfied,'pending_objectives':pending,
            'unmet_requirements':pending,'blocked_actions':(['battle_start'] if valid and not satisfied else []),
            'code':code,'summary':summary}


def runtime_capabilities(screen_allowed=False, input_allowed=False):
    """Descriptive tool availability, never a replacement for local guards."""
    screen, inputs=screen_allowed is True, input_allowed is True
    available=screen and inputs
    return {'schema_version':1,'planning':True,'public_research':True,'local_knowledge':True,
            'roster_read':True,'roster_export_import':True,'roster_gui_scan':False,
            'maa_gui_read':screen,'maa_gui_import':available,
            'battle_start':available,'maa_loaded_job_start':available,'game_navigation':available,
            'execution_scope':'loaded_single_job_once','direct_game_input':False,
            'permissions':{'screen_allowed':screen,'maa_input_allowed':inputs},
            'unavailable_actions':([] if available else ['battle_start','game_navigation'])
                + ['direct_game_input','roster_gui_scan','multi_battle_start','unbounded_battle'],
            'note':'可在本次用户明确启动指令下核对已导入单作业、只导航到对应关卡并启动MAA一次。'
                '这里只描述工具能力，不授予当前任务权限；仅查询/导入任务仍不能开战。'
                '缺权限可继续只读研究，具体GUI动作由本地合同、画面与提交门核对。'}
