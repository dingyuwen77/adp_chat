"""企业微信有序图文规划：直接断言内容顺序、完整性和链接边界。"""

import unittest
from html import escape, unescape

from wecom.reply_plan import ImageBlock, TextBlock, reply_plan, text_parts


class ReplyPlanTests(unittest.TestCase):
    """图片解析不执行网络操作，也不能改变终端和 API 的原始内容。"""

    def test_markdown_and_structured_images_keep_sources_and_tail(self):
        """带管道查询及括号的截图链接留在原位置，普通来源不是图片。"""
        url = "https://lke.cloud.tencent.com/s/one?size=mid|432*246|0.24&x=(a)"
        source = "参考来源：[说明](https://adp.cloud.tencent.com/preview?id=1)"
        record = {"adp_state": "ADP_SUCCEEDED", "notification": "旧通知不能替代正式内容",
                  "adp_result": {"reply_contents": [[
                      {"Type": "text", "Text": "步骤一\n![截图](" + url + ")\n步骤二"},
                      {"Type": "image", "Image": {"Url": "https://lke.cloud.tencent.com/two.png"}},
                      {"Type": "text", "Text": source + "\n服务台联系方式"}]]}}
        self.assertEqual(reply_plan(record), [TextBlock("步骤一\n"), ImageBlock(url, "截图"),
            TextBlock("\n步骤二"), ImageBlock("https://lke.cloud.tencent.com/two.png", ""),
            TextBlock(source + "\n服务台联系方式")])

    def test_code_and_escaped_images_are_literal(self):
        """代码示例与转义图片不得触发主动下载。"""
        text = r"\![示意](https://lke.cloud.tencent.com/a.png)" + "\n`![代码](https://lke.cloud.tencent.com/b.png)`"
        self.assertEqual(reply_plan({"notification": text}), [TextBlock(text)])

    def test_image_destination_whitespace_title_and_code_fences(self):
        """合法留白/title不留下多余括号，围栏中的Markdown示例保持原文。"""
        url = "https://lke.cloud.tencent.com/a.png"
        for syntax in (f'![x]( {url}  )', f'![x](<{url}> "标题")', f'![x]({url} "标题")'):
            self.assertEqual(reply_plan({"notification": "前" + syntax + "后"}),
                             [TextBlock("前"), ImageBlock(url, "x"), TextBlock("后")])
        code = f"```markdown\n![x]({url})\n```\n"
        self.assertEqual(reply_plan({"notification": code}), [TextBlock(code)])

    def test_whitespace_between_markdown_and_structured_images_is_not_a_message(self):
        """图片前后及相邻图片间的纯空白不成为文字气泡，原始结果不被改写。"""
        first, second = 'https://example.com/one.png', 'https://example.com/two.png'
        markdown = f'\n\n![一]({first})\n \t\u3000\n![二]({second})\n\n'
        self.assertEqual(reply_plan({'notification': markdown}), [ImageBlock(first, '一'), ImageBlock(second, '二')])
        record = {'adp_state': 'ADP_SUCCEEDED', 'notification': markdown,
                  'adp_result': {'reply_contents': [[{'Type': 'image', 'Image': {'Url': first}},
                      {'Type': 'text', 'Text': '\n \t\n'}], [{'Type': 'image', 'Image': {'Url': second}}]]}}
        original = repr(record)
        self.assertEqual(reply_plan(record), [ImageBlock(first), ImageBlock(second)])
        self.assertEqual(repr(record), original)

    def test_blank_text_and_long_whitespace_chunks_are_never_sent(self):
        """纯空白通知及长消息切出的纯换行片全部跳过，有正文的空白保持原样。"""
        for blank in ('\n\n', ' \t\r\n', '\u00a0\u3000', '\n' * 5000):
            with self.subTest(blank_length=len(blank)):
                self.assertEqual(reply_plan({'notification': blank}), [])
                self.assertEqual(text_parts(blank), [])
        normal = '\n\n正文\n\n    缩进与段落\n\n'
        self.assertEqual(text_parts(normal), [normal])
        for text in ('\n' * 5000 + '正文', '正文' + '\n' * 5000, '\n' * 2048 + '[来源](https://example.com/)'):
            with self.subTest(length=len(text)):
                parts = text_parts(text)
                self.assertTrue(parts)
                self.assertTrue(all(part.strip() and len(part.encode()) <= 2048 for part in parts))
                self.assertTrue(any('正文' in part or '<a href=' in part for part in parts))

    def test_paragraph_and_url_boundaries_preserve_all_bytes(self):
        """来源转为完整超链接后整体移动，正文与段落边界保留。"""
        url = "https://adp.cloud.tencent.com/preview?" + "x" * 300
        text = "甲" * 600 + "\n\n" + "[来源](" + url + ")\n尾部"
        parts = text_parts(text)
        self.assertEqual("".join(parts), "甲" * 600 + "\n\n" + f'<a href="{url}">来源</a>\n尾部')
        self.assertTrue(all(len(p.encode()) <= 2048 for p in parts))
        self.assertTrue(any(url in p for p in parts))
        self.assertTrue(parts[0].endswith("\n\n"))

    def test_oversized_single_url_has_explicit_fallback(self):
        """平台无法容纳的单个链接明确说明，不发截断且无法打开的 URL。"""
        parts = text_parts("前文 https://example.com/" + "x" * 2100 + " 后文")
        self.assertIn("链接过长", "".join(parts))
        self.assertTrue(all(len(p.encode()) <= 2048 for p in parts))
        self.assertTrue("".join(parts).endswith(" 后文"))

    def test_oversized_markdown_link_preserves_unspaced_tail(self):
        """超长来源地址降级时不能吞掉右括号后紧邻的中文与服务台信息。"""
        text = "[来源](https://example.com/" + "x" * 2050 + ")。服务台联系方式"
        rendered = "".join(text_parts(text))
        self.assertIn("链接过长", rendered)
        self.assertTrue(rendered.endswith("）。服务台联系方式"))

    def test_all_inline_text_links_convert_without_changing_the_reply_plan(self):
        """任意位置的普通链接在发送分段时转换，持久化输入和图文规划保留原文。"""
        text = "请查看：[Windows_修改密码](https://example.com/help?a=1&b=2)\n相关：[MFA](http://example.com/mfa)"
        record = {"notification": text}
        self.assertEqual(reply_plan(record), [TextBlock(text)])
        self.assertEqual(text_parts(text), ['请查看：<a href="https://example.com/help?a=1&amp;b=2">Windows_修改密码</a>\n'
                                           '相关：<a href="http://example.com/mfa">MFA</a>'])
        self.assertEqual(record["notification"], text)

    def test_balanced_destinations_titles_and_escaped_labels(self):
        """括号、尖括号目的地、title和名称转义正确解析，尾文不能被吞掉。"""
        cases = [
            ('[说明](https://example.com/a_(b)?x=(c))尾文', '说明', 'https://example.com/a_(b)?x=(c)'),
            ('[说明]( <https://example.com/a?x=1&y=2> "标题" )尾文', '说明', 'https://example.com/a?x=1&y=2'),
            (r'[说明\[一\]](https://example.com/a\(b\))尾文', '说明[一]', 'https://example.com/a(b)'),
            ('[说明[一]](https://example.com/a)尾文', '说明[一]', 'https://example.com/a'),
            ('''[<说明>&"'](https://example.com/a?x="a"&y='b')尾文''', '<说明>&"\'', '''https://example.com/a?x="a"&y='b' '''.strip()),
            ('[说明](https://example.com/a "标题")尾文', '说明', 'https://example.com/a'),
            ('[说明](https://example.com/a "标题(一")尾文', '说明', 'https://example.com/a'),
            ('[说明](https://example.com/a "标题)一")尾文', '说明', 'https://example.com/a'),
            ('[说明](https://example.com/?ids=[1]&x=2)尾文', '说明', 'https://example.com/?ids=[1]&x=2'),
        ]
        for source, label, url in cases:
            with self.subTest(source=source):
                self.assertEqual(text_parts(source), [f'<a href="{escape(url, quote=True)}">{escape(label, quote=False)}</a>尾文'])

    def test_code_images_escaped_and_malformed_links_remain_literal(self):
        """图片、代码、转义与畸形语法不被误转成文字超链接。"""
        samples = [r'\[示例](https://example.com/a)', r'\![示例](https://example.com/a)',
                   '![截图](https://example.com/a.png)', '`[示例](https://example.com/a)`',
                   '```markdown\n[示例](https://example.com/a)\n```',
                   '~~~\n[示例](https://example.com/a)\n~~~',
                   '    [示例](https://example.com/a)\n', '\t[示例](https://example.com/a)\n',
                   '[示例](https://example.com/a', '[示例](https://example.com/a invalid)',
                   '[示例](https://example.com/a "未闭合)', '[示例](<https://example.com/a> garbage)']
        for source in samples:
            with self.subTest(source=source):
                self.assertEqual(unescape(''.join(text_parts(source))), source)
                self.assertNotIn('<a ', ''.join(text_parts(source)))

    def test_unsafe_schemes_controls_and_raw_html_cannot_create_anchors(self):
        """危险协议与原始HTML只作为文字输出，不接受属性或标签注入。"""
        samples = ['[名称](javascript:alert(1))', '[名称](data:text/html,boom)',
                   '[名称](//example.com/a)', '[名称](https://)',
                   '[名称](https://example.com/\x00a)', '[名称](https://example.com/\x80a)',
                   '[名称](https://user:pass@example.com/a)',
                   '[名称](java&#115;cript:alert(1))', '<a href="javascript:alert(1)">原始标签</a>',
                   '<a href="https://example.com/">原始链接</a>']
        for source in samples:
            with self.subTest(source=source):
                rendered = ''.join(text_parts(source))
                self.assertNotIn('<', rendered)
                self.assertEqual(unescape(rendered), source)

    def test_markdown_entities_are_decoded_once_then_escaped_for_wecom(self):
        """目的地实体不能双重编码，实体编码的属性注入仍被封在href内。"""
        source = '[A &amp; B](https://example.com/?a=1&amp;b=&quot;x&quot;)'
        self.assertEqual(text_parts(source), ['<a href="https://example.com/?a=1&amp;b=&quot;x&quot;">A &amp; B</a>'])

    def test_anchor_and_entities_are_atomic_at_utf8_boundaries(self):
        """按实际转义后字节分段，标签与实体都完整，名称中的自然边界不能截断链接。"""
        anchor = '<a href="https://example.com/?a=1&amp;b=2">来源 一。二</a>'
        source = '[来源 一。二](https://example.com/?a=1&b=2)'
        for prefix in ('甲' * 670, 'x' * (2048 - len(anchor.encode())), 'x' * 2047):
            parts = text_parts(prefix + source + '<&>尾文')
            self.assertEqual(''.join(parts), prefix + anchor + '&lt;&amp;&gt;尾文')
            self.assertTrue(any(anchor in part for part in parts))
            self.assertTrue(all(0 < len(part.encode()) <= 2048 for part in parts))
            self.assertTrue(all(part.count('<a ') == part.count('</a>') for part in parts))
        self.assertEqual(text_parts('x' * 2047 + '&尾文'), ['x' * 2047, '&amp;尾文'])

    def test_oversized_anchor_degrades_to_plain_label_and_url(self):
        """单标签超过上限时保留名称和可容纳地址，不生成半个标签。"""
        label = '名称' * 400
        url = 'https://example.com/a'
        parts = text_parts(f'[{label}]({url})尾文')
        self.assertEqual(''.join(parts), f'{label}（{url}）尾文')
        self.assertTrue(all(len(part.encode()) <= 2048 and '<a ' not in part for part in parts))
        self.assertTrue(any(url in part for part in parts))

    def test_single_anchor_at_exact_limit_and_escape_expansion_overflow(self):
        """2048字节标签可以整体发送，多一个字节或转义膨胀后降级也不能截断标签。"""
        prefix = 'https://example.com/'
        overhead = len(f'<a href="{prefix}">名</a>'.encode())
        url = prefix + 'x' * (2048 - overhead)
        expected = f'<a href="{url}">名</a>'
        self.assertEqual(len(expected.encode()), 2048)
        self.assertEqual(text_parts(f'[名]({url})'), [expected])
        self.assertEqual(''.join(text_parts(f'[名]({url}x)')), f'名（{url}x）')
        rendered = ''.join(text_parts('[名](https://example.com/?' + '&' * 500 + ')尾文'))
        self.assertIn('链接过长', rendered)
        self.assertTrue(rendered.endswith('）尾文'))
        self.assertNotIn('<a ', rendered)

    def test_many_unclosed_image_openers_have_linear_character_reads(self):
        """未闭合及嵌套但无效的目的地都不能反复扫描或复制相同尾部。"""
        class CountedText(str):
            """计数直接逐字符访问，稳定捕捉二次方尾部重扫而不依赖机器速度。"""

            reads = 0

            def __getitem__(inner, index):
                """计入逐字符读取及切片复制长度，不依赖机器速度。"""
                if isinstance(index, int):
                    inner.reads += 1
                elif isinstance(index, slice):
                    start, end, step = index.indices(len(inner))
                    inner.reads += len(range(start, end, step))
                return super().__getitem__(index)

        for sample in ("![x](" * 500, "![x]( " * 500 + ")" * 500,
                       "![x](" * 500 + " " * 500 + ")" * 500,
                       "![x](" * 500 + "<" + " " * 500 + ">" + ")" * 500,
                       "`" * 3500, "![[" * 1000):
            text = CountedText(sample)
            self.assertEqual(reply_plan({"notification": text}), [TextBlock(text)])
            self.assertLessEqual(text.reads, len(text) * 10)

        for sample in ('[x](' * 500, '[x]( ' * 500 + ')' * 500,
                       '[x](' * 500 + ' ' * 500 + ')' * 500,
                       '[x]( "' * 500 + ')' * 500):
            text = CountedText(sample)
            self.assertEqual(''.join(text_parts(text)), sample)
            self.assertLessEqual(text.reads, len(text) * 10)

    def test_unknown_reply_uses_saved_notice_and_unsupported_is_visible(self):
        """未知结果只通知风险，正式不支持的媒体在原位置有说明。"""
        contents = [[{"Type": "video"}, {"Type": "text", "Text": "尾部"}]]
        record = {"adp_state": "ADP_UNKNOWN", "notification": "执行结果未知", "adp_result": {"reply_contents": contents}}
        self.assertEqual(reply_plan(record), [TextBlock("执行结果未知")])
        record["adp_state"] = "ADP_SUCCEEDED"
        self.assertIn("未能呈现", "".join(p.text for p in reply_plan(record)))

    def test_adjacent_adp_text_parts_are_parsed_together(self):
        """ADP连续文字块拆开图片语法时，先合并文字再识别原位置图片。"""
        url = "https://lke.cloud.tencent.com/a.png"
        record = {"adp_state": "ADP_SUCCEEDED", "notification": "旧提示", "adp_result": {"reply_contents": [[
            {"Type": "text", "Text": "步骤![截图]("}, {"Type": "json_text", "Text": url},
            {"Type": "text", "Text": ")尾部"}]]}}
        self.assertEqual(reply_plan(record), [TextBlock("步骤"), ImageBlock(url, "截图"), TextBlock("尾部")])


if __name__ == "__main__":
    unittest.main()
