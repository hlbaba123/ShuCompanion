import unittest
from shu.network_dialogue import _defer_job_research


class JobRouteEfficiencyTests(unittest.TestCase):
    def test_only_io_deferral_not_fixed_intent_or_answer(self):
        for text in ('在作业站找TL-6作业','查询TL-10并导入作业','获取新活动的作业','下载完整JSON'):
            self.assertTrue(_defer_job_research(text))
        for text in ('今天有什么活动','你好','什么是MAA作业格式','刷六次不吃药',
                     '帮我查一下MAA作业格式的最新规范','帮我搜索PRTS作业协议文档'):
            self.assertFalse(_defer_job_research(text))


if __name__=='__main__':unittest.main()
