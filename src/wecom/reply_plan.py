"""只规划企业微信输出；保留正式 reply 顺序，不改变 ADP/API/终端内容。"""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from html import escape, unescape
from urllib.parse import urlsplit


_URL = re.compile(r"https?://", re.I)
_REFERENCE = re.compile(r"!?\[")
_MARKDOWN_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])")
_LONG_LINK = "[链接过长，无法在企业微信文字消息中完整展示]"


def _url_ranges(text: str) -> list[tuple[int, int]]:
    """在 URL 自身平衡括号处结束，不吞入 Markdown 右括号后的正文。"""
    ranges, consumed = [], 0
    for match in _URL.finditer(text):
        if match.start() < consumed:
            continue
        round_depth, square_depth, end = 0, 0, len(text)
        for index in range(match.end(), len(text)):
            char = text[index]
            if char.isspace() or char in '<>"\'。！？；，（）':
                end = index
                break
            if char == "(":
                round_depth += 1
            elif char == ")":
                if not round_depth:
                    end = index
                    break
                round_depth -= 1
            elif char == "[":
                square_depth += 1
            elif char == "]":
                if not square_depth:
                    end = index
                    break
                square_depth -= 1
        ranges.append((match.start(), end))
        consumed = end
    return ranges


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """保护围栏、缩进代码和行内反引号；游标前进，畸形长串不触发尾部回溯。"""
    fences = list(re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)", text))
    ranges, index = [], 0
    while index < len(fences):
        opening = fences[index]
        delimiter = opening.group(1)
        end = len(text)
        index += 1
        while index < len(fences):
            closing = fences[index]
            index += 1
            if (closing.group(1)[0] == delimiter[0] and len(closing.group(1)) >= len(delimiter)
                    and not text[closing.end(1):closing.end()].strip()):
                end = closing.end()
                break
        ranges.append((opening.start(), end))
    # 缩进内容保守作为代码保护，避免将示例变成可点击链接或主动下载图片。
    ranges.extend((match.start(), match.end()) for match in re.finditer(r"(?m)^(?: {4}|\t)[^\n]*(?:\n|$)", text))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    ranges = merged
    runs = list(re.finditer(r"`+", text))
    next_run, next_same = {}, {}
    for index in range(len(runs) - 1, -1, -1):
        length = len(runs[index].group())
        if length in next_run:
            next_same[index] = next_run[length]
        next_run[length] = index
    inline, index, fence_index = [], 0, 0
    while index < len(runs):
        start = runs[index].start()
        while fence_index < len(ranges) and ranges[fence_index][1] <= start:
            fence_index += 1
        if fence_index < len(ranges) and ranges[fence_index][0] <= start:
            index += 1
            continue
        closing = next_same.get(index)
        if closing is not None and (fence_index == len(ranges) or runs[closing].end() <= ranges[fence_index][0]):
            inline.append((start, runs[closing].end()))
            index = closing + 1
        else:
            index += 1
    return sorted(ranges + inline)


@dataclass(frozen=True)
class TextBlock:
    """等待按企业微信字节限制分段的原始文字。"""

    text: str


@dataclass(frozen=True)
class ImageBlock:
    """原位置的图片引用，下载或上传失败时使用安全文字降级。"""

    url: str
    alt: str = ""

    def fallback(self) -> str:
        """保留可打开的 HTTP(S) 原链接，不把危险协议变成可点击内容。"""
        url = self.url if self.url.lower().startswith(("https://", "http://")) else "（图片地址不可用）"
        return f"[图片未能显示，请打开链接查看]\n{url}"


def text_parts(text: str) -> list[str]:
    """出站转换安全链接并按字节分段；跳过纯空白片，保留正文格式及完整链接。"""
    if not isinstance(text, str) or not text:
        raise ValueError("empty notification")
    rewritten, protected, offset = [], [], 0
    for value, atomic in _wecom_text_units(text):
        rewritten.append(value)
        if atomic:
            protected.append((offset, offset + len(value)))
        offset += len(value)
    text = "".join(rewritten)
    unit_starts = [a for a, _ in protected]
    raw = text.encode("utf-8")
    parts, start, byte_start = [], 0, 0
    while start < len(text):
        end = start + len(raw[byte_start:byte_start + 2048].decode("utf-8", errors="ignore"))
        if end < len(text):
            unit_index = bisect_right(unit_starts, end) - 1
            if unit_index >= 0 and protected[unit_index][0] < end < protected[unit_index][1]:
                end = protected[unit_index][0]
            # 自然边界过近会产生大量短消息；只在当前容量后半区寻找。
            floor = start + (end - start) // 2
            for separator in ("\n\n", "\n", "。", "！", "？", " "):
                boundary = text.rfind(separator, floor, end)
                cut = boundary + len(separator)
                unit_index = bisect_right(unit_starts, cut) - 1
                inside_unit = unit_index >= 0 and protected[unit_index][0] < cut < protected[unit_index][1]
                if boundary >= floor and not inside_unit:
                    end = cut
                    break
        parts.append(text[start:end])
        byte_start += len(parts[-1].encode("utf-8"))
        start = end
    return [part for part in parts if part.strip()]


def _markdown_references(text: str, *, images_only: bool = False):
    """图文和文字链接共用一次索引；跳过代码/转义，保持嵌套括号和尾文边界。"""
    cursor, code_index = 0, 0
    codes = _code_ranges(text)
    matches = [match for match in _REFERENCE.finditer(text) if not images_only or text[match.start()] == "!"]
    reference_starts = [m.start() for m in matches]
    whitespace = list(re.finditer(r"\s+", text))
    space_starts = [m.start() for m in whitespace]
    space_ends = [m.end() for m in whitespace]
    angles = [i for i, char in enumerate(text) if char == ">"]
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    escaped_positions, escaped, quotes = set(), False, []
    for index, char in enumerate(text):
        if escaped:
            escaped_positions.add(index)
        elif char in "\"'":
            quotes.append(index)
        escaped = char == "\\" and not escaped
    next_quote, quote_ends = {}, {}
    for index in reversed(quotes):
        char = text[index]
        if char in next_quote:
            quote_ends[index] = next_quote[char]
        next_quote[char] = index
    stacks, closing, ignored_until = {"(": [], "[": []}, {}, 0
    # 两种括号独立建索引；合法title内的括号是文字，不参与目的地配对。
    for index, char in enumerate(text):
        if index < ignored_until or index in escaped_positions:
            continue
        if char in "\"'" and index and text[index - 1].isspace() and stacks["("]:
            quote_end = quote_ends.get(index)
            if quote_end is not None:
                after = quote_end + 1
                space_index = bisect_right(space_starts, after) - 1
                if space_index >= 0 and after < space_ends[space_index]:
                    after = space_ends[space_index]
                if text[after:after + 1] == ")":
                    ignored_until = quote_end + 1
                    continue
        if char in stacks:
            stacks[char].append(index)
        elif char in ")]":
            stack = stacks["(" if char == ")" else "["]
            if stack:
                closing[stack.pop()] = index
    for match in matches:
        while code_index < len(codes) and codes[code_index][1] <= match.start():
            code_index += 1
        if (match.start() < cursor or match.start() in escaped_positions or code_index < len(codes)
                and codes[code_index][0] <= match.start() < codes[code_index][1]):
            continue
        label_start = match.end()
        label_end = closing.get(label_start - 1)
        if label_end is None or text[label_end + 1:label_end + 2] != "(":
            continue
        line_index = bisect_left(newlines, label_start)
        if line_index < len(newlines) and newlines[line_index] < label_end:
            continue
        opening = label_end + 1
        end = closing.get(opening)
        if end is None:
            continue
        syntax_end = end + 1
        start = opening + 1
        # 空白游程只扫描/索引一次；嵌套候选共享空白区时不能重复逐字符遍历。
        space_index = bisect_right(space_starts, start) - 1
        if space_index >= 0 and start < space_ends[space_index]:
            start = min(end, space_ends[space_index])
        space_index = bisect_right(space_starts, end - 1) - 1
        if space_index >= 0 and end <= space_ends[space_index]:
            end = max(start, space_starts[space_index])
        if start == end:
            continue
        space_index = bisect_left(space_starts, start)
        first_space = space_starts[space_index] if space_index < len(space_starts) else len(text)
        # CommonMark 的 <URL> 及可选 title；管道等查询参数不得按分隔符截断。
        if text[start] == "<":
            angle_index = bisect_left(angles, start)
            if angle_index == len(angles) or angles[angle_index] >= end:
                continue
            url_start, url_end = start + 1, angles[angle_index]
            title = url_end + 1
        else:
            url_start, url_end = start, min(first_space, end)
            title = url_end
        space_index = bisect_right(space_starts, title) - 1
        if space_index >= 0 and title < space_ends[space_index]:
            title = min(end, space_ends[space_index])
        # 先用索引判断无效嵌套目的地，再复制；不能为每个候选复制同一长后缀。
        if url_start == url_end or first_space < url_end:
            continue
        # 非HTTP目的地内的嵌套候选先排除，避免畸形输入反复复制长后缀。
        candidate_index = bisect_left(reference_starts, url_start)
        if (candidate_index < len(matches) and reference_starts[candidate_index] < url_end
                and not text[url_start:url_start + 8].lower().startswith(("http://", "https://"))):
            continue
        if title < end:
            if text[title] in "\"'":
                title_end = quote_ends.get(title)
            else:
                title_end = closing.get(title) if text[title] == "(" else None
            if title_end != end - 1:
                continue
        destination = text[url_start:url_end]
        yield match.start(), syntax_end, text[label_start:label_end], destination, text[match.start()] == "!"
        cursor = syntax_end


def _markdown(text: str) -> list[TextBlock | ImageBlock]:
    """只提取原位置图片；文字原文留给统一出站渲染，避免污染持久化结果。"""
    blocks, cursor = [], 0
    for start, end, label, destination, _ in _markdown_references(text, images_only=True):
        if start > cursor:
            blocks.append(TextBlock(text[cursor:start]))
        blocks.append(ImageBlock(destination, label))
        cursor = end
    if cursor < len(text):
        blocks.append(TextBlock(text[cursor:]))
    return blocks


def _literal_units(text: str):
    """转义所有原始标签和实体；普通 URL 按实际发送长度整体保护或明确降级。"""
    cursor = 0
    for start, end in _url_ranges(text):
        for char in text[cursor:start]:
            yield escape(char, quote=False), char in "&<>"
        value = escape(text[start:end], quote=False)
        yield value if len(value.encode("utf-8")) <= 2048 else _LONG_LINK, True
        cursor = end
    for char in text[cursor:]:
        yield escape(char, quote=False), char in "&<>"


def _safe_link(url: str) -> bool:
    """仅接受无控制字符、空白、凭据或反斜杠的绝对 HTTP(S) 目标，不发起访问。"""
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\\<>" for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None and parsed.port != 0)
    except ValueError:
        return False


def _wecom_text_units(text: str):
    """安全文字链接是唯一生成HTML的入口，超大标签退为名称与原地址的普通文字。"""
    cursor = 0
    for start, end, label, destination, image in _markdown_references(text):
        if image:
            continue
        url = unescape(_MARKDOWN_ESCAPE.sub(r"\1", destination))
        if not _safe_link(url):
            continue
        yield from _literal_units(text[cursor:start])
        label = unescape(_MARKDOWN_ESCAPE.sub(r"\1", label))
        anchor = f'<a href="{escape(url, quote=True)}">{escape(label, quote=False)}</a>'
        if len(anchor.encode("utf-8")) <= 2048:
            yield anchor, True
        else:
            yield from _literal_units(f"{label}（{url}）")
        cursor = end
    yield from _literal_units(text[cursor:])


def reply_plan(record: dict) -> list[TextBlock | ImageBlock]:
    """优先正式持久化内容；未知/忙碌通知只使用其已保存的风险说明。"""
    result = record.get("adp_result") or {}
    messages = result.get("reply_contents") if record.get("adp_state") == "ADP_SUCCEEDED" else None
    blocks, pending = [], []
    if messages and any(messages):
        for message in messages:
            if blocks or pending:
                pending.append("\n")
            for part in message:
                kind = part.get("Type")
                if kind in ("text", "json_text"):
                    pending.append(part["Text"])
                    continue
                if pending:
                    blocks.extend(_markdown("".join(pending)))
                    pending.clear()
                if kind == "image":
                    image = part.get("Image")
                    url = image.get("Url") if isinstance(image, dict) else None
                    blocks.append(ImageBlock(url) if isinstance(url, str) and url else
                                  TextBlock("[图片未能显示：缺少可用地址]"))
                else:
                    blocks.append(TextBlock("[本回复含当前渠道未能呈现的媒体内容]"))
        if pending:
            blocks.extend(_markdown("".join(pending)))
            pending.clear()
    else:
        blocks = _markdown(record["notification"])
    merged = []
    for block in blocks:
        if isinstance(block, TextBlock):
            pending.append(block.text)
            continue
        if pending:
            merged.append(TextBlock("".join(pending)))
            pending.clear()
        merged.append(block)
    if pending:
        merged.append(TextBlock("".join(pending)))
    # 图片之间只用于排版的换行不能独立发成气泡；有正文的块不strip，保留格式。
    planned = merged or [TextBlock(record["notification"])]
    return [block for block in planned if not isinstance(block, TextBlock) or block.text.strip()]
