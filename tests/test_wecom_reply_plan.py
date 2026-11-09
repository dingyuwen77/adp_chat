"""企业微信有序图文规划：直接断言内容顺序、完整性和链接边界。"""

import unittest

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

    def test_paragraph_and_url_boundaries_preserve_all_bytes(self):
        """正常链接整体移到下一气泡，重组与原文完全一致。"""
        url = "https://adp.cloud.tencent.com/preview?" + "x" * 300
        text = "甲" * 600 + "\n\n" + "[来源](" + url + ")\n尾部"
        parts = text_parts(text)
        self.assertEqual("".join(parts), text)
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
        self.assertTrue(rendered.endswith(")。服务台联系方式"))

    def test_many_unclosed_image_openers_have_linear_character_reads(self):
        """畸形 Markdown 不能反复扫描同一尾部而阻塞共享事件循环。"""
        class CountedText(str):
            """计数直接逐字符访问，稳定捕捉二次方尾部重扫而不依赖机器速度。"""

            reads = 0

            def __getitem__(inner, index):
                """切片不计入逐字符扫描量。"""
                if isinstance(index, int):
                    inner.reads += 1
                return super().__getitem__(index)

        text = CountedText("![x](" * 500)
        self.assertEqual(reply_plan({"notification": text}), [TextBlock(text)])
        self.assertLessEqual(text.reads, len(text) * 10)

    def test_unknown_reply_uses_saved_notice_and_unsupported_is_visible(self):
        """未知结果只通知风险，正式不支持的媒体在原位置有说明。"""
        contents = [[{"Type": "video"}, {"Type": "text", "Text": "尾部"}]]
        record = {"adp_state": "ADP_UNKNOWN", "notification": "执行结果未知", "adp_result": {"reply_contents": contents}}
        self.assertEqual(reply_plan(record), [TextBlock("执行结果未知")])
        record["adp_state"] = "ADP_SUCCEEDED"
        self.assertIn("未能呈现", "".join(p.text for p in reply_plan(record)))


if __name__ == "__main__":
    unittest.main()
