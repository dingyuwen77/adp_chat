"""无协议状态的文字追问推断；规则取保守交集，不冒充服务端状态。"""

import re


_TASK_PREFIX = r"^(?:(?:请|麻烦)(?:您|你)?|帮我|帮忙|先|再|\s)*"
_META_TASK = re.compile(
    _TASK_PREFIX + r"(?:翻译(?=成|为|这|那|下|以|一|[：:\s]|$)|译成|润色|改写|造句|仿写|"
    r"(?:写|拟|生成|列出|设计|给(?:我)?(?:一个|一组|几个|几条|一些|出)).{0,20}"
    r"(?:问句|问题|追问|询问|示例|例子|问卷|面试题|话术|模板)|"
    r"(?:把|将).{1,80}(?:翻译(?:成|为)|改写|列成清单|整理成(?:清单|列表)|(?:转换|换成|转成|转写|改成).{0,12}(?:表达|措辞|文案|说法)))", re.I,
)
_EXAMPLE = re.compile(r"^(?:示例|例子|样例|范例|引用|原文|原句|模板|example|sample)\s*[:：]", re.I)
_CONDITIONAL = re.compile(r"如果|假如|若(?:是|有|需|仍|还)?|如需|如有|必要时|需要时|随时|欢迎|\bif\b|\bwhenever\b", re.I)
_OPTIONAL = re.compile(
    r"还需要(?:其他|更多|进一步的)?(?:帮助|协助|支持)|还有(?:其他|什么|别的)?问题|"
    r"还(?:想|要)了解|想了解更多|是否需要(?:其他|更多|进一步的)?(?:帮助|协助|支持)|"
    r"(?:您|你)(?:是否)?(?:需要|希望|想让)我(?:继续|进一步|再)?(?:介绍|解释|说明|展开|演示|讲解)|"
    r"还有什么(?:我)?(?:能|可以)(?:帮助|帮|为|协助)|你知道为什么|难道|岂|何必|"
    r"anything else|further (?:help|assistance)|would you like", re.I,
)
_QUESTION_WORD = re.compile(r"什么|哪|多少|几(?:个|天|次|号)|是否|还是|吗|呢|\b(?:what|which|where|when|how)\b", re.I)
_REQUEST = re.compile(
    r"^(?:(?:请|麻烦)(?:您|你)?(?:先|再|进一步|帮忙|帮我|一下)*|"
    r"(?:方便|能否|可否|能不能|可以)(?:您|你)?(?:先|再)?)"
    r"(?:告诉我|告知我|提供|补充|描述|说明|上传|发送|回复|选择)|^(?:告诉我|告知我)", re.I,
)
_EN_REQUEST = re.compile(r"^(?:(?:could|can|would) you\s+|please\s+)(?:provide|tell me|share|specify|choose|confirm)\b", re.I)
_DIRECT = re.compile(
    r"^(?:请问|想先问|我想确认|您|你|具体(?:出现|是|需要)|"
    r"(?:系统|页面|客户端|发票|报销|出差|日期|城市|金额|截图|文件|图片).{0,40}|"
    r"(?:which|what|when|where)\b)", re.I,
)
_CONFIRM = re.compile(r"^(?:请(?:您|你)?(?:先)?确认|需要(?:您|你)确认)")
_PRELUDE = re.compile(
    r"^(?:为了[^，,。！？\n]{1,48}[，,：:]\s*|"
    r"(?:我)?(?:还)?需要(?:您|你)?(?:确认|了解|知道)(?:一下)?[:：]\s*)"
)
_MISSING = re.compile(r"^(?:还)?(?:缺少|缺失|需要补充)|^(?:截图|图片|文件|信息|日期|金额).{0,12}(?:模糊|不清晰|不足|缺失|未提供|无法辨认)")
_ANSWER_ACTION = re.compile(r"是|为|需要|应该|可以|登录|提交|点击|打开|重启|支持|包括|\b(?:is|are|must|submit|click|sign in)\b", re.I)
_OTHER_RECIPIENT = re.compile(r"(?:给|向|到|至)\s*(?:财务|采购|服务台|客服|IT|管理员|同事|系统|邮箱|[^\s，,；;]+@[^\s，,；;]+)|\bto (?:finance|support|the help\s?desk)\b", re.I)
_WORKFLOW_ACTION = re.compile(r"(?:再|然后|并|之后|接着|后)(?:请)?(?:提交|点击|登录|重启|打印|保存|报销)|(?:登录|点击|重启|打印)", re.I)
_AGENT_ACTION = re.compile(r"^(?:我|我们|(?:收到|补充|确认)[^，,；;]{0,20}后我|以便我|这样我)")
_INSTRUCTION_TASK = re.compile(
    r"(?:如何|怎么)(?:操作|使用|提交|登录|上传|申请|报销|联系)|"
    r"(?:介绍|讲解|了解|说明|查看|查询).{0,24}(?:流程|步骤)|(?:流程|步骤)(?:是什么|有哪些|怎么走)|材料清单|联系方式|"
    r"(?:需要|准备|提交|提供).{0,8}(?:什么|哪些|哪几)(?:材料|资料|文件|单据|发票|信息|证明)", re.I,
)


def _visible_text(text: str) -> str:
    """去掉作为展示材料的代码、引用和链接，避免把引文当成当前请求。"""
    text = re.sub(r"(?ms)^\s*(?:```|~~~).*?(?:^\s*(?:```|~~~)[^\n]*(?:\n|$)|\Z)", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\"", "", text)
    return re.sub(r"!?\[[^\]]*\]\([^)]*\)|https?://\S+", "", text)


def _direct_request(sentence: str) -> bool:
    """只接受直接面向当前用户的信息请求，排除条件建议和交给他人的操作。"""
    if _CONDITIONAL.search(sentence) or _OPTIONAL.search(sentence):
        return False
    sentence = re.sub(r"^(?:好的|你好|您好|可以)[，,]\s*", "", sentence)
    confirmation = bool(_CONFIRM.match(sentence))
    while match := _PRELUDE.match(sentence):
        sentence = sentence[match.end():].strip()
    if _MISSING.match(sentence) and re.search(r"请(?:您|你)?补充", sentence):
        return True
    if _REQUEST.match(sentence) or _EN_REQUEST.match(sentence):
        # 接收者与后续动作必须逐从句检查；助手自己转交信息不等于要求用户操作。
        for clause in re.split(r"[，,；;]", sentence):
            if _AGENT_ACTION.match(clause.strip()):
                continue
            if _OTHER_RECIPIENT.search(clause) or _WORKFLOW_ACTION.search(clause) or re.search(r"(?:即|便|就)可(?:以)?", clause):
                return False
        return True
    has_question = bool(re.search(r"[？?]$", sentence)) and bool(_QUESTION_WORD.search(sentence))
    prefix = re.split(r"[，,]", sentence, maxsplit=1)[0]
    asking_prefix = re.match(r"^(?:你|您)(?:问的是|想(?:了解|查询|问)|希望|需要)", prefix)
    if prefix != sentence and _ANSWER_ACTION.search(prefix) and not _QUESTION_WORD.search(prefix) and not confirmation and not asking_prefix:
        return False
    return has_question and bool(_DIRECT.match(sentence) or confirmation)


def is_text_clarification(question: str, text: str) -> bool:
    """依据完整回复识别常见直接追问；含答案或不明确的措辞默认不追问。

    这是一组可关闭的本地规则，不调用模型，不保证覆盖所有自然语言。
    原始任务要求生成/翻译文本时，宁可漏判，也不把作品内容当作交互请求。
    """
    if _META_TASK.search(question) or re.fullmatch(r"(?:谢谢|感谢|明白了|知道了|了解了|好的|就这样|不需要了)[，,。！!\s]*(?:(?:明白了|知道了|了解了|谢谢)[。！!\s]*)?", question.strip()):
        return False
    visible = _visible_text(text).strip()
    if not visible or _EXAMPLE.match(visible):
        return False
    if (
        _INSTRUCTION_TASK.search(question) and not re.search(r"失败|报错|出错|连不上|无法|打不开", question)
        and re.search(r"^(?:请|麻烦)(?:您|你)?(?:先)?(?:提供|上传|发送)", visible)
        and not re.search(r"[？?]", visible)
        and not re.search(r"告诉我|告知我|回复|发给我|(?:我|我们)才(?:能|可以)|以便我|后我", visible)
    ):
        return False
    sentences = []
    for part in re.split(r"(?<=[。！？!?])\s*|\n+", visible):
        sentence = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)、]\s*)", "", part).strip()
        if sentence:
            sentences.append(sentence)
    requests = [sentence for sentence in sentences if _direct_request(sentence)]
    if not requests:
        return False
    list_request = any(re.search(r"(?:以下|这些|如下)(?:信息|内容|选项)?[:：]$", sentence) for sentence in requests)
    for sentence in sentences:
        if sentence in requests:
            continue
        if re.fullmatch(r"(?:你好|您好|好的|可以|谢谢)[，,。！!]?", sentence):
            continue
        if re.match(r"^(?:例如|比如|可选项|选项)[:：]", sentence) or _MISSING.match(sentence):
            continue
        if list_request and not _ANSWER_ACTION.search(sentence):
            continue
        # 只要还有独立答案/步骤/说明，便不把尾部的问句变成强制补答。
        return False
    return True
