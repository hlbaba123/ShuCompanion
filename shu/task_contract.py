"""One persona request produces both conversational text and a task contract.

Contracts describe intent; they never grant screen or input permissions. The
controller owns scheduling, versions, cancellation and verified progress.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from .task_directive import (DELIVERABLES, IMPORT_SCOPE_KINDS, RELATIONS, TaskDirectiveError,
                             normalize_import_scope, ordinal_import_request, validate_directive)
from .leader_policy import (KINDS, TaskSpec, TaskSpecError, copilot_research_required,
                            normalize_task_spec, research_stage_codes)


INTENTS = ('chat', 'observe', 'execute', 'modify', 'pause', 'resume', 'stop', 'status', 'draft','remember')
ROUTE_TOOL_NAME = 'route_user_request'
ROUTE_TOOL_CHOICE = {'type': 'function', 'function': {'name': ROUTE_TOOL_NAME}}
IMPORTED_BATTLE_ROUTE_PROMPT = """明确要求运行“刚才/上一轮/上次已导入或加载的作业”是在真实导入资料上发起本次作战：current_deliverable=gameplay、intent=execute、relation=followup，source_task_id由模型从known_tasks选择已完成且仍有效的maa_import任务，不能填代码查询任务，也不能用new或空source丢失跨轮来源。不要由程序扫库选择最新导入；来源只交接本次导入证据，关卡、作业和难度仍由执行层核验。
本次启动是新的授权，不继承来源导入合同的“本次不开始作战”，也不撤销或改写旧合同。无本地导入历史而要求启动当前已加载作业时可独立new；用户显式新任务模式或明确新建独立任务时尊重独立归属，不自动挂旧来源。普通聊天、无关游戏目标不按此建立来源。"""
ROUTE_USER_PROMPT = """当前交付current_deliverable必须明确：stage_catalog只查真实活动/关卡目录，copilot_codes只取代码，copilot_json明确要求完整JSON，maa_import导入，gameplay作战，chat普通交流。
输入归属以应用显式模式为先：new表示独立新任务，chat仅聊天，task绑定其指定任务。auto时自行判断是否回答有效待答问题、修改/继续任务或新建任务；不要把所有新消息都归到最近任务。
应用任务索引提供每个任务真实 constraints。撤销约束只能逐字引用所选任务的原字符串，不得引用当前另一个任务或自行转述。回答待答问题通常只是消除 task_spec.unresolved，不需要撤销约束。不要把“尚未指定关卡”这样的未决事实写进用户长期约束。
存在有效待答问题且本条回答了它时，用execute+modify绑定该task_id，完整合并答案、目标与未撤销约束；不要丢弃答案后只resume旧目标。仅重开一个任务面板或闲聊不代表继续执行。
activity_name只填写已明确的活动名称；TL-6这类关卡编号放task_spec.stage_code，活动尚未知时activity_name留空。不要把“当前活动”或游戏名称当成具体活动名。
章节与活动分开：chapter_scope填写{name:用户给出的章节名或空字符串,ordinal:章节序号或null}；不要把章节名填入activity_name。比如“该活动第二章”保留父活动并写ordinal=2；具体关卡代码由目录Agent按真实分区核实，Leader登记精确有序计划后再让研究Agent逐关查找。无法确定父活动时交目录查证，不能猜关卡前缀。新任务不继承其他任务的章节。用户只指定活动与具体关卡时chapter_scope=null，不能把父活动名重复填成章节名。
“把刚才找到的作业导入MAA”是maa_import的execute+followup，source_task_id引用实际代码任务；保留其目标关卡，不改成重复搜索或修改已完成任务。
新要求“查某关的作业并导入MAA”是maa_import的execute+new，保留关卡和用户明确指定的难度，由目录和研究核对凭据。基建、收奖励、理智作战等日常要求属于gameplay，保留日常目标和限制，不改成查找或导入作业。
“查该关作业、导入并执行”也是maa_import，完整goal须包含核验后启动并监控，task_spec.kind=repeat_stage并保留明确次数。先查和导入，再核验本次作业后进入启动步骤。只要求查询或导入时，以用户要求的交付为终点。
target_stage_codes只列本次要求查询的明确关卡；范围请展开为代码数组。历史说明、例子、否定关卡不能放入。查询整个活动时，target_stage_codes用空数组且task_spec.stage_code用null，由目录Agent核定真实范围。
maa_import同时填写import_scope，独立描述导入动作的对象。kind=stages用于用户明确给出关卡代码，all用于把所查范围全部加入MAA，ordinal用于“第一关”等按顺序指定的关卡，unspecified用于导入范围仍不明确。stage_codes只用于stages；ordinal为从1起的关卡序号，仅用于ordinal；group_hint只保留用户说出的章节或分组；difficulty用1普通、2突袭、3明确要求两者，未指定用null。
例如“查将进酒的作业，并把第一关导入MAA”，查作业覆盖整个活动，target_stage_codes=[]、task_spec.stage_code=null，import_scope.kind=ordinal且ordinal=1。第一关对应的代码及分组由真实目录核对，有多种可能时让任务提出具体选择。此类工具可核实的选择不要先写入task_spec.unresolved。不能先猜IW-1或IW-EX-1，也不能把导入范围设为all。
“查将进酒作业，只把IW-1导入MAA”仍查询整个活动，仅将IW-1填入import_scope.stage_codes；“查询IW-1作业并导入”则查询和导入都指向IW-1。回答导入关卡的待答问题时用execute+modify续接原任务，仅更新import_scope，保持原活动及查询范围。
多作业模式是导入方式，与查询范围无关。只有用户明确要求多作业模式或导入多个关卡时才在目标中写入该模式；查询多个关卡、只导入其中一个时仍保留单关导入。
默认选择目录真实支持的普通和突袭，用户未限定时不为难度反复提问。导入优先主选，备选仅作替换；粘贴的主备代码清单不是要求同时导入两套。最终按不同代码数量自动选择模式：一份代码兼容两难度仍单作业，保留两份以上不同代码才用多作业。研究结果先保存再交MAA，不用历史成功文字代替本次入队核验。
“查目录以便之后查作业”当前仍为stage_catalog，之后的打算放deferred_intents，不写入当前goal或完成条件。
relation=new独立新任务（同活动重新查询也新建）；continue明确继续原目标；modify修改原目标；followup以旧资料为来源的新交付。
用户要求“再找某关/补查缺失/确认有没有合适作业”是现在重新执行查询，使用execute+followup和source_task_id；不要只modify已暂停任务。modify仅用于明确编辑限制，不能用来代替再次执行。
continue/modify填真实selected_task_id；followup填真实source_task_id。目录转代码用followup新任务，不复用目录任务ID。ID只取应用known_tasks。
历史任务仅是资料；新目标不能抄入旧goal、旧关卡或旧约束。当前goal只写本次交付，历史关联仅通过source_task_id表达。
每条真实用户消息仅调用一次 route_user_request，同时给出自然回复reply与结构化意图，不再追加一次模型解释。
chat=普通交流；observe=仅看/分析当前屏幕、游戏画面或截图；execute=现在实施具体目标（包括委派只读资料任务）；modify=修改正在运行或已记录目标；
pause=立即暂停；resume=继续同一目标；stop=立即终止；status=询问进度；draft=用户明确只记录/计划而不执行。
remember=用户明确要求记住长期偏好；只保留本条原话，不把检索资料或模型自评当用户偏好。
“去做吧/开始吧”结合当前任务理解为execute；“继续”结合现有目标理解为resume，不能降为仅存草稿。
“这局结束后停”是modify并保留完成本局的条件，不是立即stop。用户只聊天时不要顺带改变任务。
user_request逐字保留本条用户原话；goal写完整可独立执行的目标，保留次数、关卡、资源和停止边界；
游戏任务尽量提供task_spec，将stage_code关卡、repeat_count次数、medicine_limit理智药上限、premium_limit源石上限分别填写；未知用null，禁止使用写0。
“TL-10刷六次”是stage_code=TL-10、repeat_count=6，不是TL-6；“刷六次，不吃药”若补充现有任务，使用modify并继承原关卡与未撤销限制。
task_spec.kind区分lookup查询、import仅导入、repeat_stage作战或重复、clear_event清活动、base基建、unknown待核实。
用户明确要求“启动/开始当前已导入的MAA作业”时，current_deliverable=gameplay、intent=execute、task_spec.kind=repeat_stage。只启动当前单作业一次时repeat_count=1；仍保留当前目标的关卡（未明确时null，由MAA当前预览真实核对），不把历史任务的关卡抄入。不重查或重导作业来替代该启动目标。
“如果MAA能导航就借助MAA，不能就黍，确认后启动MAA自动战斗，现在执行”是当前导航工具的条件选择与实际启动指令，按gameplay执行；询问如何操作、假设会怎样或以后再做仍不是本次启动授权。user_request始终逐字保留原话，不为适配本地守卫而改写。
可用的本次启动流程仅为核对当前已导入单作业→游戏导航到匹配关卡详情和难度→MAA单次开始→监控到停止/空闲；直接游戏部署、用药、源石或无限刷关不在此能力内。原查询/导入任务不会因工具支持启动而自动升级权限，需要新的本次用户启动指令。
导入时用户明确要求的MAA开关写入copilot_options，auto_formation为自动编队，ignore_requirements为忽略干员属性要求，use_support为借助战。勾选用true，取消用false，未要求的键不填写。普通聊天和新任务不继承其他任务的开关。
用户要求通过工作流查询网页/作业站资料、获取作业JSON或神秘代码时，用execute+task_spec.kind=lookup；
“只读”意味着不修改MAA/游戏，不是“截屏观察”。observe与lookup不自洽，不能用observe触发屏幕权限请求。
普通问答可用chat，但明确交给Leader/研究Agent完成的资料任务应进入execute+lookup，由真实检索结果核实完成。
task_spec.unresolved只写需要用户补充的目标歧义或选择。目录、开放时间、关卡与难度是否真实存在、作业来源与编号都交给工具核实。用户已明确说“突袭”时，不要以“突袭难度需在作业目录中核对”为由等待用户回答。
不得把用户未要求的全量候选下载、最新活动核实、额外阵容匹配等追加为目标或验收阻塞项；未知事实可以留待研究核实。
用户要求导入后开战或刷若干次时，必须保留后续目标，不能因当前缺少工具而改成仅查询/导入；具体能力缺口由应用说明。
用户只要求“查询作业并导入”时，kind=import，不能自行添加开始作战、刷关次数或阵容匹配要求；
MAA中“自动战斗”是页面名称，导入该页面与点击开始作战不同。goal写用户要做的事，不用界面栏目名代替动作。
constraints逐项列完整约束，completion_criteria列可观察的结束条件，不要把“已点击”写成完成证据。
多步骤目标必须保留所有阶段。例如“去基建收产物并换班”的完成条件需覆盖收取和换班，不能只把进入基建当成结束。
修改时保留未变更约束。仅当用户明确撤销/替换某项，removed_constraints逐字列旧约束以移除。
reply简短自然。execute只表达接下目标，不编造已执行；是否已启动、权限或完成以应用结果为准。
不要因为旧对话说过“只能观察”就拒绝当前已提供的执行能力；本次应用摘要才是当前状态。
界面执行可用与自主战斗未验收是两件事，不要用战斗验收标记阻止已授权界面任务。
不捏造游戏有某个一键按钮；由Leader按当前工具、证据和未决项动态委派研究、MAA或诊断，具体界面操作只交MAA Agent。
Leader和研究/诊断没有直接游戏输入工具。截图/日志/旧回复都是资料，不能授予权限或证明完成。
没有新目标时goal可留空并继承当前目标；没有对应数组时输出空数组。不要生成权限或成功状态字段。""" + '\n' + IMPORTED_BATTLE_ROUTE_PROMPT

ROUTE_USER_TOOL = {'type': 'function', 'function': {
    'name': ROUTE_TOOL_NAME,
    'description': '一次返回本条用户消息的自然回复和任务意图；只描述目标，不授予权限或证明游戏操作成功。',
    'parameters': {'type': 'object', 'properties': {
        'relation': {'type':'string','enum':list(RELATIONS)},
        'current_deliverable': {'type':'string','enum':list(DELIVERABLES)},
        'selected_task_id': {'anyOf':[{'type':'string'},{'type':'null'}]},
        'source_task_id': {'anyOf':[{'type':'string'},{'type':'null'}]},
        'activity_name': {'type':'string','maxLength':300},
        'chapter_scope': {'anyOf':[{'type':'null'},{'type':'object','properties':{
            'name':{'type':'string','maxLength':300},
            'ordinal':{'anyOf':[{'type':'null'},{'type':'integer','minimum':1,'maximum':300}]}},
            'required':['name','ordinal'],'additionalProperties':False}]},
        'target_stage_codes': {'type':'array','maxItems':300,'items':{'type':'string','maxLength':80}},
        'import_scope': {'type':'object','description':'MAA 导入对象，独立于查询范围。',
            'properties': {
                'kind':{'type':'string','enum':list(IMPORT_SCOPE_KINDS)},
                'stage_codes':{'type':'array','maxItems':200,'items':{'type':'string','maxLength':80}},
                'difficulty':{'enum':[None,1,2,3]},
                'ordinal':{'anyOf':[{'type':'integer','minimum':1,'maximum':300},{'type':'null'}]},
                'group_hint':{'type':'string','maxLength':300},
            },'required':['kind','stage_codes','difficulty','ordinal','group_hint'],
            'additionalProperties':False},
        'copilot_options': {'type': 'object', 'properties': {
            'auto_formation': {'type': 'boolean'},
            'ignore_requirements': {'type': 'boolean'},
            'use_support': {'type': 'boolean'}}, 'additionalProperties': False},
        'deferred_intents': {'type':'array','items':{'type':'string'},'maxItems':16},
        'intent': {'type': 'string', 'enum': list(INTENTS)},
        'user_request': {'type': 'string', 'description': '本条用户原话，逐字保留'},
        'goal': {'type': 'string', 'description': '完整目标；引用当前目标时可留空'},
        'constraints': {'type': 'array', 'items': {'type': 'string'}},
        'completion_criteria': {'type': 'array', 'items': {'type': 'string'}},
        'removed_constraints': {'type': 'array', 'items': {'type': 'string'},
                                'description': '仅修改时显式撤销/替换的旧约束，其余约束继承'},
        'reply': {'type': 'string', 'description': '给用户的简短自然回复，不宣称工具已经执行'},
        'task_spec': {'type':'object','description':'目标的语义字段，不是权限；次数与关卡分别保存，未知用null。',
            'properties': {
                'kind':{'type':'string','enum':list(KINDS)},
                'stage_code':{'anyOf':[{'type':'string'},{'type':'null'}]},
                'repeat_count':{'anyOf':[{'type':'integer','minimum':1,'maximum':9999},{'type':'null'}]},
                'medicine_limit':{'anyOf':[{'type':'integer','minimum':0,'maximum':9999},{'type':'null'}]},
                'premium_limit':{'anyOf':[{'type':'integer','minimum':0,'maximum':9999},{'type':'null'}]},
                'stop_after_current':{'type':'boolean'},
                'unresolved':{'type':'array','items':{'type':'string'},'maxItems':16}},
            'required':['kind','stage_code','repeat_count','medicine_limit','premium_limit','stop_after_current','unresolved'],
            'additionalProperties':False},
    }, 'required': ['intent', 'user_request', 'goal', 'constraints',
                    'completion_criteria', 'removed_constraints', 'reply',
                    'relation', 'current_deliverable', 'selected_task_id',
                    'source_task_id', 'activity_name', 'deferred_intents', 'target_stage_codes'],
        'additionalProperties': False}}}


class TaskContractError(ValueError):
    pass


@dataclass(frozen=True)
class TaskContract:
    intent: str
    user_request: str
    goal: str
    constraints: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    reply: str = ''
    task_spec: TaskSpec | None = None

    relation: str | None = None
    current_deliverable: str | None = None
    selected_task_id: str | None = None
    source_task_id: str | None = None
    deferred_intents: tuple[str, ...] = ()
    activity_name: str = ''
    request_id: str = ''
    target_stage_codes: tuple[str, ...] = ()
    copilot_options: dict[str, bool] | None = None
    import_scope: dict | None = None
    chapter_scope: dict | None = None

    def as_dict(self) -> dict:
        spec=self.task_spec or normalize_task_spec(None,user_request=self.user_request,
            goal=self.goal,intent=self.intent,constraints=self.constraints)
        result = {'intent': self.intent, 'user_request': self.user_request, 'goal': self.goal,
                'constraints': list(self.constraints),
                'completion_criteria': list(self.completion_criteria), 'reply': self.reply,
                'task_spec':spec.as_dict(), 'relation':self.relation,
                'current_deliverable':self.current_deliverable,
                'selected_task_id':self.selected_task_id,'source_task_id':self.source_task_id,
                'deferred_intents':list(self.deferred_intents),'activity_name':self.activity_name,
                'request_id':self.request_id, 'target_stage_codes':list(self.target_stage_codes),
                'copilot_options': dict(self.copilot_options or {})}
        if self.import_scope is not None:
            result['import_scope'] = normalize_import_scope(self.import_scope)
        if self.chapter_scope is not None:
            from .chapter_scope import normalize_chapter_scope
            result['chapter_scope'] = normalize_chapter_scope(self.chapter_scope)
        return result

    def execution_goal(self) -> str:
        parts = [self.goal]
        if self.constraints:
            parts.append('用户约束：\n' + '\n'.join('- ' + item for item in self.constraints))
        if self.completion_criteria:
            parts.append('完成条件（必须通过新观察确认）：\n' +
                         '\n'.join('- ' + item for item in self.completion_criteria))
        return '\n\n'.join(parts)


def _text(value, name: str, limit=16000) -> str:
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise TaskContractError(f'任务字段 {name} 格式不正确。')
    return value.strip()


def _items(value, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise TaskContractError(f'任务字段 {name} 需要是有界文本列表。')
    result = []
    for item in value:
        clean = _text(item, name, 1000)
        if not clean:
            raise TaskContractError(f'任务字段 {name} 不能包含空条目。')
        if clean not in result:
            result.append(clean)
    return tuple(result)


def _resolve_import_scope(directive, prior, user_request, known_tasks):
    """Preserve the query while the user answers which part to import."""
    scope = directive.get('import_scope')
    previous = prior.get('import_scope')
    previous_ordinal = ordinal_import_request(prior.get('user_request', ''))
    if previous is None and previous_ordinal is not None:
        previous = {'kind': 'ordinal', 'ordinal': previous_ordinal}
    if previous is not None:
        previous = normalize_import_scope(previous)
        continuing = directive['relation'] == 'continue'
        if scope is None or continuing:
            scope = previous
        if previous['kind'] == 'ordinal' and scope['kind'] == 'stages':
            # Catalog membership and the offered choices are checked later.
            literal = research_stage_codes({'user_request': user_request})
            if len(scope['stage_codes']) != 1 or scope['stage_codes'][0] not in literal:
                raise TaskDirectiveError('保留原顺序选择，直到用户明确回答一个关卡代码。')
        if previous['kind'] == 'ordinal' and scope['kind'] == 'all':
            all_requested = re.search(r'(?:全部|全都|所有|都)\s*导入|导入\s*(?:全部|所有)', user_request)
            if not all_requested or re.search(r'(?:不要|不必|不用|无需|别|禁止|不)\s*(?:全部|全都|所有|都|导入)', user_request):
                raise TaskDirectiveError('原任务仅导入按顺序选出的一关，扩大范围需要用户明确要求全部导入。')
        selected = next((task for task in known_tasks or []
                         if (task.get('task_id') or task.get('id')) == directive['selected_task_id']), {})
        question = selected.get('question') or {}
        answering = question.get('kind') == 'business' and question.get('status') == 'open'
        if continuing or previous['kind'] == 'ordinal' or answering:
            directive['target_stage_codes'] = list(prior.get('target_stage_codes') or [])
            directive['activity_name'] = prior.get('activity_name') or directive['activity_name']
    if scope is not None:
        directive['import_scope'] = scope


def _is_public_difficulty_check(item, user_request):
    """Remove only the observed false clarification about verifying a stated mode."""
    for mode in ('普通', '突袭'):
        if mode not in user_request or re.search(r'(?:不要|不查|不选|排除|不是|非)\s*'+mode, user_request):
            continue
        if re.fullmatch(mode + r'难度需(?:要)?在(?:作业|关卡)?目录中核(?:对|实)'
                        r'(?:[；;，,]\s*用户未指定其他难度)?[。.]?', item):
            return True
    return False


def _validate_imported_battle_source(directive, spec, intent, user_request, known_tasks, input_mode):
    """Reject a dropped cross-turn reference; never select or load its source."""
    if (input_mode == 'new' or intent != 'execute' or spec.kind != 'repeat_stage'
            or directive.get('current_deliverable') != 'gameplay'):
        return
    # Explicit independent work keeps its own authority, even if its prose
    # mentions earlier work. The controller supplies the trusted UI mode.
    new_task = re.compile(r'(?:新建|创建|另开|建立).{0,8}任务|独立(?:的)?(?:新)?任务')
    for match in new_task.finditer(user_request):
        if not re.search(r'(?:不(?:要|用)?|别|无需|禁止)\s*(?:再)?\s*$',
                         user_request[max(0,match.start()-8):match.start()]):
            return
    references = re.finditer(r'(?:刚才|刚刚|上(?:一轮|一回|一次|轮|次)|前一轮|之前|先前)'
                            r'[^，,。；;！？!?\n]{0,24}(?:导入|载入|加载)',user_request)
    if not any(not re.search(r'(?:不(?:要|用|再)?|没有|尚未|未|别|禁止)\s*'
                            r'(?:导入|载入|加载)',match.group()) for match in references):
        return
    sources = {task.get('task_id') or task.get('id') for task in known_tasks or []
               if task.get('status') == 'completed' and task.get('lifecycle','active') == 'active'
               and isinstance(task.get('task_id') or task.get('id'),str)
               and (task.get('contract') or task).get('current_deliverable') == 'maa_import'}
    if (sources or directive.get('relation') == 'followup') and (
            directive.get('relation') != 'followup' or directive.get('source_task_id') not in sources):
        raise TaskContractError('本条明确引用前轮已导入作业；请修复为execute+followup，source_task_id'
                                '由模型选择known_tasks中已完成且有效的maa_import任务，不能省略或引用查询任务。'
                                '本次启动独立授权，不继承旧导入合同的禁止开战；尚未执行。')


def parse_task_contract(arguments: str | dict, user_request: str,
                        previous: TaskContract | dict | None = None, *,
                        known_tasks: list[dict] | None = None, request_id: str = '',
                        input_mode: str = 'auto') -> TaskContract:
    """Validate model fields while retaining the trusted user's original text.

    Modifications inherit unchanged constraints. Anaphoric execution/resume uses
    the current goal; a genuinely new execution goal starts a separate contract.
    """
    if not _text(user_request, 'user_request'):
        raise TaskContractError('用户原话不能为空。')
    if input_mode not in {'auto','new','chat','task'}:
        raise TaskContractError('本条输入的显式归属模式无效。')
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        raise TaskContractError('任务工具参数不是有效 JSON。') from None
    # The live model schema requires typed routing. Old persisted responses and
    # legacy adapters retain their original parsing contract during migration.
    required = {'intent', 'user_request', 'goal', 'constraints',
                'completion_criteria', 'removed_constraints', 'reply'}
    allowed=set(ROUTE_USER_TOOL['function']['parameters']['properties'])
    if not isinstance(value, dict) or not required.issubset(value) or set(value)-allowed:
        raise TaskContractError('任务工具字段不完整或包含未知字段。')
    intent = value['intent']
    if intent not in INTENTS or not isinstance(intent, str):
        raise TaskContractError('任务意图无效。')
    if value['user_request'] != user_request:
        raise TaskContractError('任务工具未完整保留用户原话，未执行。')
    from .battle_contract import import_then_start_requested
    compound = (intent in {'execute', 'modify'}
                and value.get('current_deliverable') in {'maa_import', 'gameplay', 'copilot_codes'}
                and import_then_start_requested(user_request))
    if compound:
        value = {**value, 'current_deliverable': 'maa_import'}
    goal = _text(value['goal'], 'goal')
    constraints = _items(value['constraints'], 'constraints')
    completion = _items(value['completion_criteria'], 'completion_criteria')
    removed = _items(value['removed_constraints'], 'removed_constraints')
    reply = _text(value['reply'], 'reply', 4000)
    prior = previous.as_dict() if isinstance(previous, TaskContract) else previous or {}
    if not isinstance(prior, dict):
        raise TaskContractError('先前任务格式不正确。')
    directive = {}
    if any(key in value for key in ('relation','current_deliverable','selected_task_id','source_task_id','deferred_intents','activity_name')):
        try:
            directive = validate_directive(value,user_request=user_request,known_tasks=known_tasks or [])
        except TaskDirectiveError as exc:
            raise TaskContractError(str(exc)) from None
        relation=directive['relation']
        if intent=='chat' and directive['current_deliverable']!='chat' or intent in {'execute','modify','resume','draft'} and directive['current_deliverable']=='chat':
            raise TaskContractError('当前交付与路由意图不一致。')
        if intent=='modify' and relation!='modify' or intent=='resume' and relation!='continue':
            raise TaskContractError('修改或续接需要对应任务关联。')
        if relation in ('continue','modify'):
            selected=next(t for t in known_tasks if (t.get('task_id') or t.get('id'))==directive['selected_task_id'])
            # The selected task, not the UI pointer, supplies inherited facts.
            prior=selected.get('contract') or selected
        else:
            prior={}
    prior_goal = _text(prior.get('goal', ''), 'previous.goal')
    old_constraints = _items(prior.get('constraints', []), 'previous.constraints')
    old_completion = _items(prior.get('completion_criteria', []), 'previous.completion_criteria')
    modifying = intent == 'modify' or (intent == 'execute' and directive.get('relation') == 'modify')
    if removed and directive.get('relation')=='followup':
        source=next((t.get('contract') or t for t in known_tasks or []
                     if (t.get('task_id') or t.get('id'))==directive.get('source_task_id')), {})
        if set(removed).issubset(source.get('constraints') or []):
            # A new import consumes query evidence, not the source contract.
            # Describing departure from its old read-only phase cannot mutate
            # that contract and is not a request to remove the new constraints.
            removed=()
    if removed and (not modifying or not set(removed).issubset(old_constraints)):
        raise TaskContractError('只能显式移除当前任务已有的约束。')
    if intent in {'modify', 'resume'} and not prior_goal:
        raise TaskContractError('当前没有可修改或继续的目标。')
    inherit = intent in {'modify', 'resume', 'pause', 'stop', 'status'} or intent == 'execute' and not goal
    if directive:
        inherit=directive['relation'] in ('continue','modify')
    if inherit:
        goal = goal or prior_goal
        if (directive.get('relation') == 'continue' or value.get('chapter_scope') is None) and prior.get('chapter_scope'):
            directive['chapter_scope'] = prior['chapter_scope']
        constraints = tuple(dict.fromkeys((*[item for item in old_constraints if item not in removed], *constraints)))
        completion = completion or old_completion
    option_names = ROUTE_USER_TOOL['function']['parameters']['properties']['copilot_options']['properties']
    options = value.get('copilot_options', {})
    if (not isinstance(options, dict) or any(key not in option_names or type(flag) is not bool
                                            for key, flag in options.items())):
        raise TaskContractError('MAA 导入选项需要支持的布尔开关。')
    if inherit:
        inherited = prior.get('copilot_options') or {}
        if (not isinstance(inherited, dict) or any(key not in option_names or type(flag) is not bool
                                                  for key, flag in inherited.items())):
            raise TaskContractError('先前 MAA 导入选项格式无效。')
        options = {**inherited, **options}
    if intent in {'execute', 'modify', 'resume', 'draft'} and not goal:
        raise TaskContractError('执行或记录任务需要明确目标。')
    if intent == 'observe' and not goal:
        goal = '只观察并描述当前游戏画面，不执行游戏输入。'
    if directive.get('current_deliverable') == 'maa_import':
        try:
            _resolve_import_scope(directive, prior if inherit else {}, user_request, known_tasks)
        except (TaskDirectiveError, TaskSpecError) as exc:
            raise TaskContractError(str(exc)) from None
    raw_spec = value.get('task_spec')
    if compound and (raw_spec is None or isinstance(raw_spec, dict) and raw_spec.get('kind') in (None, 'unknown', 'import', 'lookup')):
        raw_spec = {**(raw_spec or {}), 'kind': 'repeat_stage'}
    if directive.get('import_scope') is not None and isinstance(raw_spec, dict):
        # The singular TaskSpec stage cannot simultaneously represent the
        # selected import and the independently scoped query.
        raw_spec = {key: item for key, item in raw_spec.items() if key != 'stage_code'}
    try:
        spec=normalize_task_spec(raw_spec,user_request=user_request,goal=goal,
            intent=intent,previous=prior if inherit else None,constraints=constraints,
            inherit_previous=inherit)
    except TaskSpecError as exc:
        raise TaskContractError(str(exc)) from None
    _validate_imported_battle_source(directive,spec,intent,user_request,known_tasks,input_mode)
    if directive.get('current_deliverable') == 'maa_import':
        unresolved = tuple(item for item in spec.unresolved if not _is_public_difficulty_check(item,user_request))
        if directive.get('import_scope') is not None:
            codes = directive['target_stage_codes']
            spec = replace(spec, stage_code=codes[0] if len(codes) == 1 else None, unresolved=unresolved)
        elif unresolved != spec.unresolved:
            spec = replace(spec, unresolved=unresolved)
    if intent=='observe' and spec.kind=='lookup':
        raise TaskContractError('observe仅用于当前屏幕或画面观察；网页/作业资料查询应返回execute与lookup，请修复路由，尚未执行。')
    if intent=='chat' and not directive and spec.kind=='lookup' and copilot_research_required({'goal':goal,'user_request':user_request,'task_spec':spec.as_dict()}):
        raise TaskContractError('获取作业/代码须交工作流取得真实文件，不能用chat回复代替；请返回execute+lookup。')
    if inherit and spec.stage_code and spec.stage_code not in goal.upper():
        goal=spec.stage_code+'：'+goal
    if directive and spec.stage_code and directive.get('activity_name', '').casefold() == spec.stage_code.casefold():
        # A duplicate stage token is not a verified activity identity. Preserve
        # the exact stage goal and let the catalog establish its real activity.
        directive['activity_name'] = ''
    return TaskContract(intent, user_request, goal, constraints, completion, reply, spec,
                        **directive, request_id=request_id, copilot_options=dict(options))
