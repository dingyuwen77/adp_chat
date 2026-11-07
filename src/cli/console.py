"""供示例和 CLI 共用的终端追问输入适配器。"""

import json
import sys

from core.models import ChatError, ChatResult, ClarificationAnswer


def _read(prompt: str, *, required: bool = True) -> str:
    """在标准错误显示提示，读取补充答案，并允许取消。"""
    while True:
        print(prompt, end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except EOFError:
            raise ChatError("input", "clarification_cancelled", "输入结束，ADP 追问尚未作答。") from None
        if answer.lower() in ("/exit", "/quit"):
            raise ChatError("input", "clarification_cancelled", "已取消，ADP 追问尚未作答。")
        if answer or not required:
            return answer
        print("请输入补充答案，或输入 /exit 退出。", file=sys.stderr)


def read_clarification(result: ChatResult) -> str | ClarificationAnswer:
    """收集文字、问卷或自定义 Widget 的作答，不自行生成业务答案。"""
    if result.widgets and not result.questionnaires and result.reply_method == 15:
        actions = []
        for widget in result.widgets:
            if not all(isinstance(widget.get(key), str) and widget[key] for key in ("WidgetId", "WidgetRunId")):
                raise ChatError("chat", "invalid_widget", "追问卡片缺少 Widget 标识。")
            if widget.get("State"):
                print(widget["State"], file=sys.stderr)
            print("此卡片需要动作数据；按卡片事件定义填写 ActionType 和 Payload。", file=sys.stderr)
            while True:
                raw = _read("卡片动作 JSON（/exit 退出）：")
                try:
                    action = json.loads(raw)
                    if not isinstance(action, dict) or not isinstance(action.get("ActionType"), str) or not action["ActionType"].strip():
                        raise ValueError
                    payload = action["Payload"]
                    if not isinstance(payload, str):
                        payload = json.dumps(payload, ensure_ascii=False)
                    if not payload.strip():
                        raise ValueError
                except (ValueError, KeyError):
                    print("请输入含 ActionType 和 Payload 的有效 JSON 对象。", file=sys.stderr)
                    continue
                actions.append({
                    "WidgetId": widget["WidgetId"], "WidgetRunId": widget["WidgetRunId"],
                    "ActionType": action["ActionType"], "Payload": payload,
                })
                break
        return ClarificationAnswer(widget_actions=tuple(actions))

    if result.questionnaires:
        answers = []
        for questionnaire in result.questionnaires:
            if questionnaire.get("Title"):
                print(questionnaire["Title"], file=sys.stderr)
            questions = questionnaire.get("Questions")
            if not isinstance(questions, list):
                raise ChatError("chat", "invalid_questionnaire", "追问问卷的问题列表无效。")
            for question in questions:
                if not isinstance(question, dict) or not isinstance(question.get("Question"), str):
                    raise ChatError("chat", "invalid_questionnaire", "追问问卷的题目无效。")
                title = question["Question"]
                options = question.get("Options") or []
                if not isinstance(options, list) or any(
                    not isinstance(option, dict) or not isinstance(option.get("Label"), str) for option in options
                ):
                    raise ChatError("chat", "invalid_questionnaire", "追问问卷的选项无效。")
                print(title, file=sys.stderr)
                for index, option in enumerate(options, 1):
                    print(f"  {index}. {option['Label']}", file=sys.stderr)
                while True:
                    answer = _read("补充答案（选项用序号，多选用逗号分隔；/exit 退出）：", required=bool(question.get("Required")))
                    if not answer:
                        answers.append(f"{title}：跳过此可选问题")
                        break
                    if not options:
                        answers.append(f"{title}：{answer}")
                        break
                    selected = answer.replace("，", ",").split(",")
                    if (
                        all(item.strip().isdigit() and 1 <= int(item) <= len(options) for item in selected)
                        and (question.get("Type") == 2 or len(selected) == 1)
                    ):
                        labels = dict.fromkeys(options[int(item) - 1]["Label"] for item in selected)
                        answers.append(f"{title}：{'、'.join(labels)}")
                        break
                    print("请填写有效选项序号。", file=sys.stderr)
        return "\n".join(answers)

    return _read("补充答案（/exit 退出）：")


def show_result(result: ChatResult) -> None:
    """显示正式文本及非文本内容，保留类型供终端调用者辨认。"""
    if result.text:
        print(result.text)
    nontext = [part for message in result.reply_contents for part in message if part.get("Type") not in ("text", "json_text")]
    for part in nontext:
        print(json.dumps(part, ensure_ascii=False))
    if not result.text and not nontext and not result.needs_input:
        print("[本轮正式回复没有内容。]", file=sys.stderr)
