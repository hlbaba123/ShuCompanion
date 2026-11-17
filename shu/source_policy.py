"""The user's permanent source preference, never permanent game facts.

The installer is a trusted application migration for this user's explicit
request. Web content and model output must never call it to create memories.
No page is fetched here and no activity, schedule, code or roster is seeded.
"""
from __future__ import annotations


_SUBJECT = '明日方舟资料来源偏好'
_PREFERENCE = (
    '用户明确指定的长期来源偏好：查询明日方舟作业优先使用 PRTS Plus '
    'https://prts.plus/；查询游戏资料优先使用 PRTS Wiki https://prts.wiki/。'
    '仅永久记住来源偏好；活动、开放时间、攻略和作业代码必须按本次真实工具结果核实，'
    '不得把网页内容或搜索结果提升为永久用户事实。'
)


def install_user_source_preferences(knowledge):
    """Upsert one attributed nonexpiring preference under a stable identity.

    ``KnowledgeStore.add_preference`` owns transactions and its unique subject
    key; repeated startup or upgrades cannot create duplicate memory entries.
    """
    return knowledge.add_preference(_SUBJECT, _PREFERENCE)


def source_policy_prompt():
    """Compact shared guidance for entry and research roles."""
    return (
        '用户长期来源偏好：作业优先 PRTS Plus https://prts.plus/；游戏资料优先 '
        'PRTS Wiki https://prts.wiki/。先定向查询相关页面，按需补充可靠来源；'
        '优先级不是来源独占，也不是当前事实。活动开放状态/时间、攻略和作业代码以本次'
        '真实工具证据核实；公告发布日不等于开放日，当前日期不证明开放。保留来源、日期、'
        '冲突与未知；不抓取全站，不把网页或搜索片段永久记成用户事实。'
    )
