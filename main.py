"""大肥鱼·主动私聊插件

让 bot 在合适的时候主动找人说话（默认找 AstrBot 管理员）：
- 晨起：她醒来后 5~30 分钟，带着刚醒的劲儿来一句（睡眠期有来言→交给晨间冲刷，dm 不重复报到）
- 睡前：今晚入睡点前几分钟，今晚聊过→必收尾；没聊→概率晚安
- 随机想念 v1.5：**静默驱动**——冷场够久（三档阈值：离席45/约定或聊嗨断线90/普通120，含事件微抖）
  → 本静默事件掷一次骰（概率=base×日期档×回复反馈×热度档×离席折扣，总下限0.30，
  叠每日微扰±20%与心情节律±15%）→ 中签抖动后过 AI 否决层（只有否决权）→
  五道门终检（睡觉/时段/熔断/频控/活跃终检）→ 拟人发送
- 概率衰减：触发后 2~3 天内逐日回血；回复快慢反馈（秒回不罚/慢回小罚/没回再降/主动开话回满）
- 大小周/节假日/调休日历；显式抑制词；熔断（连2条未回暂停）；决策日志与 token 台账全记账
- 作息联动：直接读睡觉插件状态——睡着绝不发；评估时段默认自动跟作息（醒来+1h~入睡-30min，硬边界08:00~23:30）
- /主动私聊状态 查看；/主动私聊测试 立刻来一条（均仅管理员）
"""

import asyncio
import json
import math
import random
import re
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.session_llm_manager import SessionLLMManager
except ImportError:
    SessionLLMManager = None

PLUGIN_NAME = "astrbot_plugin_dafeiyu_dm"
PLUGIN_VERSION = "2.1.0"
SLEEP_STATE_CANDIDATES = [
    Path("data/plugin_data/astrbot_plugin_dafeiyu_sleep/sleep_state.json"),
]
TICK_SECONDS = 30
NIGHT_WINDOW_MIN = 10  # 入睡点前多少分钟内发睡前消息
ABS_TOTAL_CAP = 7     # 跨类型每日总量绝对上限（安全件，不可配）
QUIET_HARD_LO = 8 * 60    # 评估时段硬边界 08:00
QUIET_HARD_HI = 23 * 60 + 30  # …~23:30（风控：深夜不评估不发送）
AFK_GRACE_MIN = 5.0  # 离席/约定词宽限：词说出后对话在5分钟内结束→仍按该档（"吃饭啦""拜拜"分开发不丢档）
MOOD_STEP = 0.15        # 心情节律随机游走幅度 ±15%
DRIFT_STEP = 0.20       # 每日参数微扰幅度 ±20%

DEFAULTS = {
    "dm_enabled": True,
    "dm_targets": [],          # 留空 = 自动用 AstrBot 管理员列表
    "dm_platform": "",         # 留空 = 自动发现第一个平台
    "dm_owner_alias": "对方",   # 模板变量 {who}：对方怎么称呼收信人（大肥鱼填"主人"）
    "dm_alias_map": [],           # 按 QQ 配称呼：["10001=主人"]，没配的用全局
    "dm_seg_enabled": True,      # 主动消息按句切分逐条发（拟人+防风控）
    "dm_tools_morning_on": False,   # 晨起问候允许模型调工具（默认关）
    "dm_tools_morning_names": [],   # 晨起工具白名单，空=全部已注册
    "dm_tools_night_on": False,     # 睡前晚安允许工具（补发同此）
    "dm_tools_night_names": [],
    "dm_tools_random_on": False,    # 随机想念允许工具
    "dm_tools_random_names": [],
    "dm_two_stage": True,        # 工具路径两段式：先无工具酝酿内心戏（不发送不归档），再带工具表达
    "dm_active_gate_on": True,   # 随机想念活跃度闸门：目标正在聊天则按住不发
    "dm_active_gate_window": 15,     # 多少分钟内说过话算"正聊着"
    "dm_active_gate_defer_min": 25,  # 按住后随机重试时刻的下限（分钟）
    "dm_active_gate_defer_max": 90,  # 上限——随机顺延让人猜不出她会什么时候来
    "dm_with_context": True,    # 生成时附带该会话最近聊天记录（接话茬）
    "dm_context_count": 10,
    "dm_respect_sleep": True,
    "dm_morning_on": True,
    "dm_night_on": True,
    "dm_random_count": 2,
    "dm_window_start": "10:00",
    "dm_window_end": "22:00",
    "dm_min_gap_hours": 2.0,
    "dm_daily_cap": 4,
    "dm_morning_template": (
        "你刚睡醒没多久，想起了{who}，想主动说句话。就一两句——"
        "说什么、什么语气按你们的相处和最近聊天的氛围自己定；"
        "刚醒的状态自然带出来就好，不用刻意演。别提任何系统或作息数字。"
    ),
    "dm_night_template": (
        "你马上要去睡了，睡前想跟{who}道个别。就一句——"
        "晚安只是选项，说法按你们此刻聊天的氛围来；带着真实的睡意就好。"
        "别提任何系统或作息数字。"
    ),
    "dm_random_template": (
        "你白天突然想起了{who}，想主动跟{who}说句话。一两句，随手一条的自然感——"
        "内容与语气按你们的相处和最近聊天的氛围自己定，"
        "可以从你正在做的事、或接着最近聊过的话茬说起。别提系统。"
    ),
    "dm_fallback_pool": [
        "在吗？突然想到你了",
        "忙不忙？闲着说两句话",
        "想起个事，你那边怎么样",
        "冒个泡，证明我还活着",
        "没啥事，就是来看看你回没回消息",
    ],
    "dm_makeup_night_template": (
        "你刚躺下准备睡了，临睡前才想起来还没跟{who}道晚安，现在补上。"
        "就一句——说法按你们的相处氛围来，带着点迷糊。"
        "别提任何系统或作息数字。"
    ),
    "dm_night2_template": (
        "你们刚才已经道过晚安（或道别）了。你现在真的躺下要睡了——"
        "按你们刚才聊天的氛围，跟{who}发一句自然的收尾：语气跟着氛围走，"
        "平常聊就平常收，腻歪着收尾就接着那个劲儿，别端着客套。"
        "别再说一遍晚安，就一句，带着困意，别提任何系统或作息数字。"
    ),
    # —— v1.5.0 静默想你 ——
    "dm_miss_mode": "silence",        # silence=静默驱动 / timer=旧版定时抽签 / off=关闭随机想念
    "miss_silence_min": 120,          # 普通档阈值（分钟）
    "miss_mid_min": 90,               # 中档：约定未赴 / 聊嗨断线
    "miss_afk_min": 45,               # 吃饭档：离席词命中
    "miss_temperament": "自定义",      # 平淡/正常/热情/病娇/自定义/自定义槽名：懒人挡位一键接管数值
    "miss_tier_per_conf": False,       # 挡位按配置文件/QQ区分（v1.7.0）
    "miss_tier_conf_map": [],          # "配置ID前缀=挡位" / "QQ=挡位" / "default=挡位"
    "miss_custom_slots": [],           # 自定义挡位槽（v1.7.2，最多3个）："名称: 参数=值, 参数=值…"
    "miss_base_p": 0.6,
    # —— v2.0.0 概率引擎换代 ——
    "miss_rw_alpha": 0.30,            # Rescorla-Wagner 学习率：你的行为多快改变她的期待值
    "miss_rw_r_fast": 1.0,            # 奖励值：秒回/慢回/没回各值多少
    "miss_rw_r_slow": 0.60,
    "miss_rw_r_none": 0.15,
    "miss_rw_lo": 0.70,               # 期待值V∈[0,1]→概率乘子的映射区间
    "miss_rw_hi": 1.20,
    "miss_cd_a": 0.65,                # 冷却折扣深度：次日 f=1-a
    "miss_cd_tau": 0.9,               # 冷却恢复时间常数（天），空闲日×0.5
    "miss_haz_k": 2.0,                # Weibull 形状（风险随静默加速上升）
    "miss_haz_scale": 1.0,            # 危险度尺度乘子（性格整体左移/右移触发曲线）
    "miss_daily_max": 1,              # 想你每日上限（1~5）
    "miss_reply_fast_min": 30,        # 秒回界线
    "miss_reply_slow_min": 720,       # 超过这数算晾着（12h）
    "miss_floor": 0.30,               # 四因子连乘后的总下限
    "miss_heat_on": True,             # 热度因子总开关
    "miss_heat_cold_max": 2,          # 冷场折扣在此轮数内线性淡出
    "miss_heat_hot_min": 10,          # ≥此轮数=聊嗨档（语义标签/饭档池/重想次数仍用）
    "miss_heat_k": 10,                # 饱和常数：几轮算热络（热度响应半饱和点）
    "miss_heat_cold_factor": 0.70,    # 零轮冷场折扣（饱和曲线下限）
    "miss_heat_hot_factor": 1.25,     # 聊嗨渐近上浮（饱和曲线上限）
    "miss_heat_burst_gap": 90,        # 一轮=连续来言间隔小于此秒数
    "miss_heat_window": 120,          # 热度统计窗口（分钟）
    "miss_free_mode": "weekend",      # weekend / pattern(大小周) / off
    "miss_free_anchor": "",           # 大小周：某个大周第一天的日期 YYYY-MM-DD
    "miss_free_weekdays": ["周六", "周日"],
    "miss_free_weekdays_big": ["周日"],
    "miss_free_weekdays_small": ["周六", "周日"],
    "miss_free_dates": [],            # 手动节假日表 "10-01" 或 "2026-10-01"
    "miss_busy_dates": [],            # 调休补班表（优先级最高）
    "miss_afk_words": ["吃饭", "干饭", "洗澡", "洗漱", "出门", "取快递", "上课", "上班", "下班", "加班", "打游戏"],
    "miss_promise_words": ["晚点找你", "等会找你", "等会儿找你", "待会找你", "待会聊", "晚上找你", "晚上聊", "回头找你", "回来找你"],
    "miss_night_words": ["晚安", "睡了", "先睡", "去睡", "睡觉啦", "睡觉了", "睡啦"],  # 你先道别→她躺下必收尾（不因她睡晚漏掉）
    "miss_night_words_from": "20:00",  # 道别词从几点开始算数（再配合同晚8h闸双保险）
    "miss_together_words": ["一起", "连麦", "牵着", "抱着", "陪你"],  # 在一起语境词：窗口内主动话术避开"想你/等你"防穿帮
    "miss_together_min": 150,          # 在一起语境持续分钟（10~480 夹取）
    "miss_mute_words": ["别发了", "别找我", "别烦我", "烦不烦", "让我静静"],
    "miss_mute_days": 3,              # 显式抑制持续天数（或主动开话2次解冻）
    "miss_retry_gap": "3-6",         # 长冷场重想间隔（小时"低-高"，每次单独抽）
    "miss_retry_tau": 5,              # 想念半衰期（小时）：重想概率=U0/(1+t/τ)双曲衰减，τ随热度伸缩
    "miss_jitter": "10-40",           # 抖动期"低-高"（分钟），可 0-0
    "miss_drift": True,               # 每日参数微扰 ±20%
    "miss_quiet_mode": "auto",        # auto=跟睡眠作息 / manual=用手动时段
    "miss_quiet_start": "10:00",
    "miss_quiet_end": "22:30",
    "miss_active_check": 3,           # 活跃终检窗（分钟），0=关（不建议）
    "miss_greet_gap": 20,             # 早晚安近距查重（分钟）
    "miss_circuit_breaker": 2,        # 连续几条主动未回→熔断
    "miss_ai_veto": False,            # AI 否决层（用户实例可开；依赖两段式）
    "miss_prompt_tools": "",          # 用工具守则（空=内置默认）
    "dm_gen_mode_morning": "ai",      # ai / template
    "dm_gen_mode_night": "ai",
    "dm_gen_mode_random": "ai",
    "dm_tools_scenario_map": [],      # 情景→工具许可："吃饭=deliver_music,draw_doodle"
    "dm_miss_random_template": (
        "你突然想起了{who}，想主动跟{who}说句话。一两句，随手一条的自然感——"
        "内容与语气按你们的相处和最近聊天的氛围自己定。别提系统。"
    ),
    "dm_miss_veto_hint": (
        "发之前先自问一句：从最近聊天的内容和氛围看，这会儿主动找{who}合不合时宜——"
        "TA刚说过在忙/在赶工/心情很差、或你们刚闹了不愉快，就别凑上去："
        "第一行只写【不发】加一句理由，后面什么都不要写。"
        "其他情况第一行写【发】，然后接着写要说的话。"
        "TA暂时离席（吃饭/洗澡/出门/下班路上）不算在忙——离席多久、该不该等，"
        "背景事实里有：离席期间发消息不会打扰，TA回来自会看到。"
        "拿不准就【发】——时机系统已经把过关，你只看内容和氛围。"
    ),
    "dm_miss_stance_hint": (
        "你是TA身边亲近的人，不是助理。想找TA说句话很正常，"
        "不用替TA找「可能打扰」的理由——时机该不该，规则层已经把关。"
        "只在你真的觉得TA此刻不方便时才按住。"
    ),
    "dm_miss_stance_gen": False,     # 一次性心态段生成器：开=按当前人设生成一次→写回→自动关回
    "miss_stance_conf_map": [],      # "配置ID前缀=心态段" / "QQ=心态段" / "default=心态段"（按人分流，同挡位映射）
    "miss_free_p_mult": 1.2,         # 空闲日基础概率乘子（周末/节假日/扣除调休）
    "miss_free_thr_mult": 0.8,       # 空闲日触发阈值乘子（<1=更早起评）
}

# 性格预设映射（只动这四个键，其余因子不碰）
# 懒人挡位（v1.6.0）：ov=选了该挡位后一键覆盖的参数终值（都已按夹取范围给）。
# 数值锚点：依恋理论——回避型低频短消息/焦虑型高频长消息+过度寻求确认(Evraire&Dozois 2022)，
# 平淡≈回避型、正常≈安全型(=全部默认值)、热情≈焦虑型轻症；病娇=焦虑型极端化的"重い愛"演绎，全高。
# 保险丝层（评估时段硬边界/熔断/抑制词/硬顶7）任何挡位都不碰——人味归挡位，等得起归保险丝。
_TEMPERAMENTS = {
    "平淡": {"morning_base": 0.5, "evening_base": 0.35, "ov": {
        "c_base_p": 0.35, "c_daily_max": 1, "c_cap": 3, "c_random_n": 1,
        "c_rw_alpha": 0.40, "c_rw_r_fast": 0.80, "c_rw_r_slow": 0.30, "c_rw_r_none": 0.00,
        "c_rw_lo": 0.55, "c_rw_hi": 0.95,
        "c_cd_a": 0.65, "c_cd_tau": 3.0, "c_floor": 0.15,
        "c_retry_gap_lo": 6.0, "c_retry_gap_hi": 12.0, "c_retry_tau": 2.0,
        "c_heat_cold_f": 0.50, "c_heat_hot_f": 1.10, "c_heat_k": 14,
        "c_afk_min": 60, "c_mid_min": 120, "c_silence_min": 180,
        "c_haz_scale": 1.5, "c_gap": 3.0}},
    "正常": {"morning_base": 0.7, "evening_base": 0.5, "ov": {
        "c_base_p": 0.6, "c_daily_max": 1, "c_cap": 4, "c_random_n": 2,
        "c_rw_alpha": 0.30, "c_rw_r_fast": 1.00, "c_rw_r_slow": 0.60, "c_rw_r_none": 0.15,
        "c_rw_lo": 0.70, "c_rw_hi": 1.20,
        "c_cd_a": 0.65, "c_cd_tau": 0.9, "c_floor": 0.30,
        "c_retry_gap_lo": 3.0, "c_retry_gap_hi": 6.0, "c_retry_tau": 5.0,
        "c_heat_cold_f": 0.70, "c_heat_hot_f": 1.25, "c_heat_k": 10,
        "c_afk_min": 45, "c_mid_min": 90, "c_silence_min": 120,
        "c_haz_scale": 1.0, "c_gap": 2.0}},
    "热情": {"morning_base": 0.85, "evening_base": 0.65, "ov": {
        "c_base_p": 0.75, "c_daily_max": 2, "c_cap": 5, "c_random_n": 3,
        "c_rw_alpha": 0.35, "c_rw_r_fast": 1.00, "c_rw_r_slow": 0.75, "c_rw_r_none": 0.40,
        "c_rw_lo": 0.85, "c_rw_hi": 1.30,
        "c_cd_a": 0.50, "c_cd_tau": 1.0, "c_floor": 0.40,
        "c_retry_gap_lo": 2.0, "c_retry_gap_hi": 4.0, "c_retry_tau": 8.0,
        "c_heat_cold_f": 0.85, "c_heat_hot_f": 1.45, "c_heat_k": 7,
        "c_afk_min": 35, "c_mid_min": 75, "c_silence_min": 90,
        "c_haz_scale": 0.75, "c_gap": 1.0}},
    "病娇": {"morning_base": 0.95, "evening_base": 0.8, "ov": {
        "c_base_p": 0.90, "c_daily_max": 4, "c_cap": 7, "c_random_n": 5,
        "c_rw_alpha": 0.15, "c_rw_r_fast": 1.00, "c_rw_r_slow": 0.95, "c_rw_r_none": 0.85,
        "c_rw_lo": 0.95, "c_rw_hi": 1.45,
        "c_cd_a": 0.25, "c_cd_tau": 0.5, "c_floor": 0.55,
        "c_retry_gap_lo": 1.0, "c_retry_gap_hi": 2.0, "c_retry_tau": 12.0,
        "c_heat_cold_f": 1.00, "c_heat_hot_f": 1.70, "c_heat_k": 4,
        "c_afk_min": 25, "c_mid_min": 50, "c_silence_min": 60,
        "c_haz_scale": 0.5, "c_gap": 0.5}},
    "自定义": {"morning_base": 0.7, "evening_base": 0.5, "ov": {}},
}
# 旧挡位名迁移：淡泊/黏人是用户主动选过的性格→映射新名保留意图；标准是旧默认→转自定义，保住手动调过的数值
_TEMPERAMENT_MIGRATE = {"淡泊": "平淡", "标准": "自定义", "黏人": "热情"}
# 自定义挡位槽可写的参数键 → (c_属性/None=基线, 下限, 上限, 类型)；值按同款夹取范围收口
_CUSTOM_KEYS = {
    "miss_base_p": ("c_base_p", 0.05, 1.0, float),
    "miss_daily_max": ("c_daily_max", 1, 5, int),
    "dm_daily_cap": ("c_cap", 1, 7, int),
    "miss_rw_alpha": ("c_rw_alpha", 0.05, 1.0, float),
    "miss_rw_r_fast": ("c_rw_r_fast", 0.0, 1.0, float),
    "miss_rw_r_slow": ("c_rw_r_slow", 0.0, 1.0, float),
    "miss_rw_r_none": ("c_rw_r_none", 0.0, 1.0, float),
    "miss_rw_lo": ("c_rw_lo", 0.3, 1.5, float),
    "miss_rw_hi": ("c_rw_hi", 0.4, 2.0, float),
    "miss_cd_a": ("c_cd_a", 0.0, 0.95, float),
    "miss_cd_tau": ("c_cd_tau", 0.2, 7.0, float),
    "miss_haz_k": ("c_haz_k", 1.2, 4.0, float),
    "miss_haz_scale": ("c_haz_scale", 0.3, 2.0, float),
    "miss_floor": ("c_floor", 0.05, 0.9, float),
    "miss_retry_tau": ("c_retry_tau", 1.0, 12.0, float),
    "miss_heat_cold_factor": ("c_heat_cold_f", 0.1, 2.0, float),
    "miss_heat_hot_factor": ("c_heat_hot_f", 1.0, 2.0, float),
    "miss_heat_k": ("c_heat_k", 2, 30, int),
    "miss_afk_min": ("c_afk_min", 10, 120, int),
    "miss_mid_min": ("c_mid_min", 15, 240, int),
    "miss_silence_min": ("c_silence_min", 20, 480, int),
    "morning_base": (None, 0.05, 1.0, float),
    "evening_base": (None, 0.05, 1.0, float),
}
_WEEKDAY_NAMES = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}


def _parse_hhmm(value):
    try:
        s = str(value).strip()
        h, m = s.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def _clamp_lo_hi(lo: int, hi: int) -> tuple:
    """硬边界内夹紧（评估时段 08:00~23:30）"""
    lo = min(QUIET_HARD_HI, max(QUIET_HARD_LO, lo))
    hi = min(QUIET_HARD_HI, max(QUIET_HARD_LO, hi))
    if hi <= lo:
        hi = min(QUIET_HARD_HI, lo + 30)
    return lo, hi


@register(
    PLUGIN_NAME,
    "c29624",
    "主动私聊：晨起/睡前/随机想念，联动睡觉插件作息，她睡着不打扰；可挂任意llm工具",
    PLUGIN_VERSION,
)
class DafeiyuDmPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        cfg = self._flatten(config or {})
        g = lambda k: cfg.get(k, DEFAULTS[k])
        self.enabled = bool(g("dm_enabled"))
        self.c_targets = [str(t).strip() for t in (g("dm_targets") or []) if str(t).strip()]
        self.c_platform = str(g("dm_platform") or "").strip()
        self.c_alias = str(g("dm_owner_alias") or DEFAULTS["dm_owner_alias"])
        # 按 QQ 配称呼：["10001=主人", ...]，没配的用全局称呼
        self.c_alias_map = {}
        for item in g("dm_alias_map") or []:
            try:
                qq, alias = str(item).split("=", 1)
                qq, alias = qq.strip(), alias.strip()
                if qq and alias:
                    self.c_alias_map[qq] = alias
            except Exception:
                continue
        self.c_with_ctx = bool(g("dm_with_context"))
        self.c_seg_on = bool(g("dm_seg_enabled"))
        # 工具调用（v1.3.0）：三路独立，各自开关+白名单（空=全部已注册 llm_tool）
        self.c_tools_morning_on = bool(g("dm_tools_morning_on"))
        self.c_tools_morning_names = [str(n).strip() for n in (g("dm_tools_morning_names") or []) if str(n).strip()]
        self.c_tools_night_on = bool(g("dm_tools_night_on"))
        self.c_tools_night_names = [str(n).strip() for n in (g("dm_tools_night_names") or []) if str(n).strip()]
        self.c_tools_random_on = bool(g("dm_tools_random_on"))
        self.c_tools_random_names = [str(n).strip() for n in (g("dm_tools_random_names") or []) if str(n).strip()]
        # v1.4.0：两段式生成 + 随机想念活跃度闸门
        self.c_two_stage = bool(g("dm_two_stage"))
        self.c_gate_on = bool(g("dm_active_gate_on"))
        self.c_gate_window = max(1, int(g("dm_active_gate_window") or 15))
        self.c_gate_defer_min = max(1, int(g("dm_active_gate_defer_min") or 25))
        self.c_gate_defer_max = max(self.c_gate_defer_min, int(g("dm_active_gate_defer_max") or 90))
        self.c_ctx_n = max(3, min(40, int(g("dm_context_count") or 10)))
        self.c_respect_sleep = bool(g("dm_respect_sleep"))
        self.c_morning = bool(g("dm_morning_on"))
        self.c_night = bool(g("dm_night_on"))
        self.c_random_n = max(0, min(5, int(g("dm_random_count") or 0)))
        self.c_win_lo = _parse_hhmm(g("dm_window_start"))
        self.c_win_lo = 10 * 60 if self.c_win_lo is None else self.c_win_lo  # 0=00:00 是合法值，不能 or 兜底
        self.c_win_hi = _parse_hhmm(g("dm_window_end"))
        self.c_win_hi = 22 * 60 if self.c_win_hi is None else self.c_win_hi
        self.c_gap = max(0.0, float(g("dm_min_gap_hours") or 0))
        self.c_cap = min(ABS_TOTAL_CAP, max(1, int(g("dm_daily_cap") or 1)))  # 硬顶7，面板填大也没用
        self.tpl_morning = str(g("dm_morning_template"))
        self.tpl_night = str(g("dm_night_template"))
        self.tpl_random = str(g("dm_random_template"))
        self.tpl_makeup_night = str(
            g("dm_makeup_night_template")
        )
        self.tpl_night2 = str(g("dm_night2_template"))
        pool = [str(s).strip() for s in (g("dm_fallback_pool") or []) if str(s).strip()]
        self.c_pool = pool or list(DEFAULTS["dm_fallback_pool"])
        # —— v1.5.0 静默想你：配置解析（概率放开、频率夹死）——
        self.c_miss_mode = str(g("dm_miss_mode") or "silence").strip().lower()
        if self.c_miss_mode not in ("silence", "timer", "off"):
            self.c_miss_mode = "silence"
        self.c_silence_min = min(480, max(20, int(g("miss_silence_min") or 120)))
        self.c_mid_min = min(240, max(30, int(g("miss_mid_min") or 90)))
        self.c_afk_min = min(120, max(15, int(g("miss_afk_min") or 45)))
        # 三档自动排序：afk < mid < silence（配乱序自动排，记一次日志）
        afk, mid, sil = self.c_afk_min, self.c_mid_min, self.c_silence_min
        self.c_afk_min, self.c_mid_min, self.c_silence_min = sorted([afk, mid, sil])
        if (self.c_afk_min, self.c_mid_min, self.c_silence_min) != (afk, mid, sil):
            logger.info(f"[主动私聊] 三档阈值配乱序({afk}/{mid}/{sil})，已自动排序为 "
                        f"{self.c_afk_min}/{self.c_mid_min}/{self.c_silence_min}")
        # v1.7.2 自定义挡位槽：最多3个命名参数包，槽名可当挡位用在全局选择和映射表里
        self._custom_tiers = {}
        for _slot in list(g("miss_custom_slots") or [])[:3]:
            _line = str(_slot).strip()
            _name, _sep, _body = _line.partition(":")
            if not _sep:
                _name, _sep, _body = _line.partition("：")
            _name = _name.strip()
            if not _name or _name in _TEMPERAMENTS or _name in self._custom_tiers:
                logger.warning(f"[主动私聊] 自定义挡位槽「{_line[:30]}」名字为空/与固定挡重名/重复，跳过")
                continue
            _ov, _m, _e = {}, None, None
            for _kv in _body.split(","):
                _k, _s2, _v = _kv.partition("=")
                _k, _v = _k.strip(), _v.strip()
                if _k == "miss_retry_gap" and _s2:
                    _lo, _hi = self._parse_jitter(_v or "3-6")
                    _ov["c_retry_gap_lo"] = min(12.0, max(1.0, float(_lo)))
                    _ov["c_retry_gap_hi"] = min(12.0, max(1.0, float(_hi)))
                    continue
                _spec = _CUSTOM_KEYS.get(_k)
                if not _spec or not _s2:
                    logger.warning(f"[主动私聊] 自定义挡位槽「{_name}」参数「{_k}」不认识，跳过")
                    continue
                _attr, _lo, _hi, _cast = _spec
                try:
                    _val = _cast(float(_v))
                except ValueError:
                    logger.warning(f"[主动私聊] 自定义挡位槽「{_name}」参数「{_k}={_v}」不是数，跳过")
                    continue
                _val = min(_hi, max(_lo, _val))
                if _attr is None:
                    if _k == "morning_base":
                        _m = _val
                    else:
                        _e = _val
                else:
                    _ov[_attr] = _val
            self._custom_tiers[_name] = {
                "morning_base": 0.7 if _m is None else _m,
                "evening_base": 0.5 if _e is None else _e,
                "ov": _ov,
            }
            if _ov or _m is not None or _e is not None:
                logger.info(f"[主动私聊] 自定义挡位槽「{_name}」已注册（{len(_ov)} 项参数）")
        temper_raw = str(g("miss_temperament") or "自定义")
        temper = _TEMPERAMENT_MIGRATE.get(temper_raw, temper_raw)  # 旧名迁移（淡泊/标准/黏人）
        self.c_temperament = temper if (temper in _TEMPERAMENTS or temper in self._custom_tiers) else "自定义"
        tp = self._tier_def(self.c_temperament)
        self.c_base_p = min(1.0, max(0.05, float(g("miss_base_p") or DEFAULTS["miss_base_p"])))
        self.c_morning_base = tp["morning_base"]
        self.c_evening_base = tp["evening_base"]
        # —— v2.0.0 概率引擎参数 ——
        self.c_rw_alpha = min(1.0, max(0.05, float(g("miss_rw_alpha") or 0.30)))
        self.c_rw_r_fast = min(1.0, max(0.0, float(g("miss_rw_r_fast") or 1.0)))
        self.c_rw_r_slow = min(1.0, max(0.0, float(g("miss_rw_r_slow") or 0.60)))
        self.c_rw_r_none = min(1.0, max(0.0, float(g("miss_rw_r_none") or 0.15)))
        self.c_rw_lo = min(1.5, max(0.3, float(g("miss_rw_lo") or 0.70)))
        self.c_rw_hi = min(2.0, max(self.c_rw_lo + 0.05, float(g("miss_rw_hi") or 1.20)))
        self.c_cd_a = min(0.95, max(0.0, float(g("miss_cd_a") or 0.65)))
        self.c_cd_tau = min(7.0, max(0.2, float(g("miss_cd_tau") or 0.9)))
        self.c_haz_k = min(4.0, max(1.2, float(g("miss_haz_k") or 2.0)))
        self.c_haz_scale = min(2.0, max(0.3, float(g("miss_haz_scale") or 1.0)))
        self.c_daily_max = min(5, max(1, int(g("miss_daily_max") or 1)))
        self.c_reply_fast = max(1, int(g("miss_reply_fast_min") or 30))
        self.c_reply_slow = max(60, int(g("miss_reply_slow_min") or 720))
        self.c_floor = min(0.9, max(0.05, float(g("miss_floor") or 0.30)))
        self.c_heat_on = bool(g("miss_heat_on"))
        self.c_heat_cold_max = min(5, max(1, int(g("miss_heat_cold_max") or 2)))
        self.c_heat_hot_min = min(50, max(4, int(g("miss_heat_hot_min") or 10)))
        if self.c_heat_hot_min < self.c_heat_cold_max + 2:  # 两档之间必须留正常带
            self.c_heat_hot_min = self.c_heat_cold_max + 2
            logger.info(f"[主动私聊] 热度两档贴脸，聊嗨线已自动拉开到 {self.c_heat_hot_min}")
        self.c_heat_cold_f = float(g("miss_heat_cold_factor") or 0.70)
        self.c_heat_hot_f = float(g("miss_heat_hot_factor") or 1.25)
        self.c_heat_k = min(30, max(2, int(g("miss_heat_k") or 10)))
        self.c_burst_gap = min(600, max(30, int(g("miss_heat_burst_gap") or 90)))
        self.c_heat_win = min(240, max(30, int(g("miss_heat_window") or 120)))
        self.c_free_mode = str(g("miss_free_mode") or "weekend").strip().lower()
        if self.c_free_mode not in ("weekend", "pattern", "off"):
            self.c_free_mode = "weekend"
        self.c_free_anchor = self._parse_date(g("miss_free_anchor"))
        self.c_free_wd = self._parse_weekdays(g("miss_free_weekdays"), [5, 6])
        self.c_free_wd_big = self._parse_weekdays(g("miss_free_weekdays_big"), [6])
        self.c_free_wd_small = self._parse_weekdays(g("miss_free_weekdays_small"), [5, 6])
        self.c_free_dates = self._parse_datelist(g("miss_free_dates"))
        self.c_busy_dates = self._parse_datelist(g("miss_busy_dates"))
        self.c_afk_words = [str(w).strip() for w in (g("miss_afk_words") or []) if str(w).strip()]
        self.c_promise_words = [str(w).strip() for w in (g("miss_promise_words") or []) if str(w).strip()]
        self.c_night_words = [str(w).strip() for w in (g("miss_night_words") or []) if str(w).strip()]
        self.c_night_from = _parse_hhmm(g("miss_night_words_from"))
        self.c_together_words = [str(w).strip() for w in (g("miss_together_words") or []) if str(w).strip()]
        self.c_together_min = min(480, max(10, int(g("miss_together_min") or 150)))
        self.c_night_from = 20 * 60 if self.c_night_from is None else self.c_night_from
        self.c_mute_words = [str(w).strip() for w in (g("miss_mute_words") or []) if str(w).strip()]
        self.c_mute_days = min(30, max(1, int(g("miss_mute_days") or 3)))
        self.c_jitter_lo, self.c_jitter_hi = self._parse_jitter(g("miss_jitter"))
        rg_lo, rg_hi = self._parse_jitter(g("miss_retry_gap") or "3-6")  # 重想间隔（小时，复用低-高解析）
        self.c_retry_gap_lo = min(12.0, max(1.0, float(rg_lo)))
        self.c_retry_gap_hi = min(12.0, max(1.0, float(rg_hi)))
        if self.c_retry_gap_hi < self.c_retry_gap_lo:
            self.c_retry_gap_lo, self.c_retry_gap_hi = self.c_retry_gap_hi, self.c_retry_gap_lo
        self.c_retry_tau = min(12.0, max(1.0, float(g("miss_retry_tau") or 5)))
        self.c_drift = bool(g("miss_drift"))
        self.c_quiet_mode = str(g("miss_quiet_mode") or "auto").strip().lower()
        if self.c_quiet_mode not in ("auto", "manual"):
            self.c_quiet_mode = "auto"
        self.c_quiet_lo = _parse_hhmm(g("miss_quiet_start"))
        self.c_quiet_lo = 10 * 60 if self.c_quiet_lo is None else self.c_quiet_lo
        self.c_quiet_hi = _parse_hhmm(g("miss_quiet_end"))
        self.c_quiet_hi = 22 * 60 + 30 if self.c_quiet_hi is None else self.c_quiet_hi
        self.c_active_check = min(60, max(0, int(g("miss_active_check") if g("miss_active_check") is not None else 3)))
        self.c_greet_gap = min(120, max(0, int(g("miss_greet_gap") or 20)))
        self.c_breaker = min(5, max(1, int(g("miss_circuit_breaker") or 2)))
        self.c_ai_veto = bool(g("miss_ai_veto")) and self.c_two_stage  # 否决住在内心戏里
        self.c_prompt_tools = str(g("miss_prompt_tools") or "").strip()
        self.c_gen_mode = {
            "morning": str(g("dm_gen_mode_morning") or "ai"),
            "night": str(g("dm_gen_mode_night") or "ai"),
            "random": str(g("dm_gen_mode_random") or "ai"),
            "makeup": str(g("dm_gen_mode_night") or "ai"),
        }
        self.c_scenario_map = self._parse_scenario_map(g("dm_tools_scenario_map"))
        # 懒人挡位兜底覆盖（v1.6.0）：选了具体挡位=数值由挡位说了算；自定义=上面的单项全生效
        if tp["ov"]:
            for _k, _v in tp["ov"].items():
                setattr(self, _k, _v)
            logger.info(f"[主动私聊] 懒人挡位「{self.c_temperament}」已接管 {len(tp['ov'])} 项数值参数"
                        f"（单项配置仅自定义挡生效）")
        # v1.7.0 挡位按配置文件/QQ区分：映射解析 + 全局参数快照（按人分流时在快照上叠挡位ov）
        self.c_tier_per_conf = bool(g("miss_tier_per_conf"))
        self._tier_map = {}
        for _item in (g("miss_tier_conf_map") or []):
            _s = str(_item).strip()
            if "=" not in _s:
                logger.warning(f"[主动私聊] 挡位映射「{_s}」没有=，跳过")
                continue
            _k, _, _v = _s.partition("=")
            _k, _v = _k.strip(), str(_TEMPERAMENT_MIGRATE.get(_v.strip(), _v.strip()))
            if _v not in _TEMPERAMENTS and _v not in self._custom_tiers:
                logger.warning(f"[主动私聊] 挡位映射「{_s}」挡位名不认识，跳过")
                continue
            self._tier_map[("qq:" + _k) if _k.isdigit() else ("conf:" + _k.lower())] = _v
        if self.c_tier_per_conf and self._tier_map:
            logger.info(f"[主动私聊] 挡位按配置文件/QQ区分已开：{sorted(self._tier_map.values())}"
                        f"（{len(self._tier_map)} 条映射）")
        self._conf_pid_cache = {}
        self._p_cache = {}
        self._p_global = self._build_p_global()
        self.tpl_miss_random = str(g("dm_miss_random_template") or DEFAULTS["dm_miss_random_template"])
        self.tpl_veto_hint = str(g("dm_miss_veto_hint") or DEFAULTS["dm_miss_veto_hint"])
        # v2.1.0 AI 感官包：心态段（判断先验，按人可分）+ 周末档乘子
        self._cfg_obj = config
        _sv = g("dm_miss_stance_hint")
        self.c_stance_hint = DEFAULTS["dm_miss_stance_hint"] if _sv is None else str(_sv).strip()
        self._stance_map = {}
        for _item in (g("miss_stance_conf_map") or []):
            _s2 = str(_item).strip()
            if "=" not in _s2:
                logger.warning(f"[主动私聊] 心态段映射「{_s2}」没有=，跳过")
                continue
            _k2, _, _v2 = _s2.partition("=")
            self._stance_map[("qq:" + _k2) if _k2.strip().isdigit() else ("conf:" + _k2.strip().lower())] = _v2.strip()
        if self._stance_map:
            logger.info(f"[主动私聊] 心态段按人分流已开（{len(self._stance_map)} 条映射，QQ/配置前缀）")
        try:
            self.c_free_p_mult = min(2.0, max(0.5, float(g("miss_free_p_mult"))))
        except Exception:
            self.c_free_p_mult = 1.2
        try:
            self.c_free_thr_mult = min(1.5, max(0.5, float(g("miss_free_thr_mult"))))
        except Exception:
            self.c_free_thr_mult = 0.8
        # 评估时段自动模式的缓存（读不到 sleep 时沿用上次推导）
        self._quiet_cache = None
        self._last_gen_status = "ok"
        self._last_veto_reason = ""
        # 当日执行状态（dm_state.json 持久化，跨重启不重发不丢计划）
        self._st = {}
        self._data_dir = None
        self._task = None
        # 本轮工具调用轨迹（R7 归档用）：每次生成都清空，发完由 _fire 拼进归档
        self._tool_trace: list = []

    @staticmethod
    def _flatten(cfg) -> dict:
        """面板分组（object 组）会存成嵌套 dict：拍平成一层供读取。
        已是平铺的旧配置/测试桩不受影响（配置里只有分组是 dict 值）。"""
        flat = {}
        for k, v in cfg.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    flat[kk] = vv
            else:
                flat[k] = v
        return flat

    # ---------- v1.7.0 挡位按配置文件/QQ区分：按人分流的有效参数 ----------

    _P_KEYS = ("base_p", "daily_max", "cap",
               "rw_alpha", "rw_r_fast", "rw_r_slow", "rw_r_none", "rw_lo", "rw_hi",
               "cd_a", "cd_tau", "floor",
               "retry_gap_lo", "retry_gap_hi", "retry_tau",
               "heat_cold_f", "heat_hot_f", "heat_k",
               "afk_min", "mid_min", "silence_min", "haz_scale",
               "morning_base", "evening_base")

    def _build_p_global(self):
        """全局参数快照（含全局挡位 ov 后的值）——开关关着时所有人共用这一份"""
        ns = SimpleNamespace()
        for k in self._P_KEYS:
            setattr(ns, k, getattr(self, "c_" + k))
        return ns

    def _tier_def(self, name: str) -> dict:
        """挡位定义：固定挡或自定义槽；都不认识回落自定义（不动数值）"""
        return _TEMPERAMENTS.get(name) or self._custom_tiers.get(name) or _TEMPERAMENTS["自定义"]

    def _P(self, umo: str):
        """该 umo 的有效参数：开关关=全局快照；开=按 QQ/配置文件解析挡位后叠加 ov"""
        if not self.c_tier_per_conf:
            return self._p_global
        tier = self._tier_of(umo)
        cached = self._p_cache.get(umo)
        if cached and cached[0] == tier:
            return cached[1]
        tp = self._tier_def(tier)
        ns = SimpleNamespace()
        for k in self._P_KEYS:
            setattr(ns, k, getattr(self._p_global, k))
        for k, v in tp["ov"].items():
            attr = k[2:]
            if hasattr(ns, attr):
                setattr(ns, attr, v)
        ns.morning_base = tp["morning_base"]
        ns.evening_base = tp["evening_base"]
        self._p_cache[umo] = (tier, ns)
        return ns

    def _tier_of(self, umo: str) -> str:
        """QQ 映射 > 配置文件映射 > 全局挡位"""
        if not self.c_tier_per_conf:
            return self.c_temperament
        qq = str(umo.split(":")[-1])
        t = self._tier_map.get("qq:" + qq)
        if t:
            return t
        cid = self._conf_of(umo)
        if cid:
            t = self._tier_map.get("conf:" + cid.lower())
            if not t:  # 支持配置ID前缀匹配（面板里那串 UUID 只写开头几位）
                for k, v in self._tier_map.items():
                    if k.startswith("conf:") and cid.lower().startswith(k[5:]):
                        t = v
                        break
        return t or self.c_temperament

    def _conf_of(self, umo: str) -> str:
        """umo → 配置文件ID：配置的 platform 列表里含该平台 → 该配置；找不到=default"""
        pid = str(umo.split(":")[0])
        if pid in self._conf_pid_cache:
            return self._conf_pid_cache[pid]
        cid = "default"
        try:
            confs = getattr(self.context.astrbot_config_mgr, "confs", {}) or {}
            for kid, conf in (confs.items() if hasattr(confs, "items") else []):
                for p in (conf.get("platform") or []):
                    if isinstance(p, dict) and str(p.get("id")) == pid:
                        cid = str(kid)
                        break
                if cid != "default":
                    break
        except Exception:
            pass
        self._conf_pid_cache[pid] = cid
        return cid

    def _drift_vals(self, umo: str) -> dict:
        """当日微扰：开关关=全局 drift；开=每人一份懒掷（围绕该人挡位的参数滚 ±20%）"""
        if not self.c_tier_per_conf:
            return self._st.get("drift") or {}
        qq = str(umo.split(":")[-1])
        today = datetime.now().date().isoformat()
        ent = (self._st.get("pdrift") or {}).get(qq)
        if ent and ent.get("date") == today and bool(ent.get("on")) == bool(self.c_drift):
            return ent.get("v") or {}
        if not self.c_drift:
            ent = {"date": today, "on": False, "v": None}
        else:
            P = self._P(umo)

            def _dr(x, lo, hi):
                v = x * random.uniform(1 - DRIFT_STEP, 1 + DRIFT_STEP)
                return min(hi, max(lo, v))

            ent = {"date": today, "on": True, "v": {
                "silence": _dr(P.silence_min, 20, 480), "mid": _dr(P.mid_min, 15, 240),
                "afk": _dr(P.afk_min, 10, 120), "base": _dr(P.base_p, 0.05, 1.0)}}
        self._st.setdefault("pdrift", {})[qq] = ent
        return ent.get("v") or {}

    def _per_cnt(self, umo: str, key: str) -> int:
        """当日按人计数（开关关=回落全局计数，键名对齐 sent_total/random_sent）"""
        if not self.c_tier_per_conf:
            gk = {"sent": "sent_total", "random": "random_sent"}.get(key, key)
            return int(self._st.get(gk, 0))
        qq = str(umo.split(":")[-1])
        return int(((self._st.get("per") or {}).get(qq) or {}).get(key, 0))

    # ---------- v2.0.0 概率引擎三件套：RW期待值 / Weibull触发 / 概率乘子 ----------

    def _rw_factor(self, umo: str) -> float:
        """期待值 V∈[0,1] → 概率乘子 [lo, hi]（性格决定区间，V 由相处养成）"""
        P = self._P(umo)
        v = float(self._sess(umo).get("exp_v", 0.6))
        return P.rw_lo + (P.rw_hi - P.rw_lo) * v

    def _rw_update(self, umo: str, r: float):
        """Rescorla-Wagner 更新：V ← V + α(r−V)。秒回/慢回/没回各是一个奖励值，
        她的期待值被你的每次回应平滑地教出来（不是阶跃 discount）"""
        P = self._P(umo)
        s = self._sess(umo)
        v = float(s.get("exp_v", 0.6))
        v = min(1.0, max(0.0, v + P.rw_alpha * (r - v)))
        s["exp_v"] = round(v, 3)
        self._dlog({"t": "rw", "umo": umo.split(":")[-1], "r": r, "v": round(v, 3),
                    "factor": round(P.rw_lo + (P.rw_hi - P.rw_lo) * v, 2)})

    def _haz_p(self, t: float, T: float) -> float:
        """Weibull 触发（v2.0.0）：静默 t 秒、尺度 T 秒，本 tick"想起你"的概率。
        瞬时风险 λ(t)=k·(t/T)^(k−1)/T，k=2 时风险随静默线性上升——
        没有"静默满 N 分钟才开始想"的到点悬崖（v1.x 是阶跃阈值+wobble）"""
        if t <= 0 or T <= 0:
            return 0.0
        h = self.c_haz_k * (t / T) ** (self.c_haz_k - 1) / T
        return 1.0 - math.exp(-TICK_SECONDS * h)

    @staticmethod
    def _parse_date(value):
        """YYYY-MM-DD → date；解析失败返回 None"""
        try:
            return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except Exception:
            return None

    @staticmethod
    def _parse_weekdays(values, default):
        out = []
        for v in values or []:
            n = _WEEKDAY_NAMES.get(str(v).strip())
            if n is not None and n not in out:
                out.append(n)
        return out or list(default)

    def _parse_datelist(self, values):
        """["10-01","2026-10-02"] → {"10-01","2026-10-02"}（两种格式都存原串，命中时任一可对上）"""
        out = set()
        for v in values or []:
            s = str(v).strip()
            if self._parse_date(s) or re.fullmatch(r"\d{2}-\d{2}", s):
                out.add(s)
        return out

    @staticmethod
    def _parse_jitter(value):
        """'10-40' → (10, 40)；'0-0' 允许；夹紧 0~120"""
        try:
            s = str(value).replace("～", "-").replace("~", "-").replace("，", "-").replace(",", "-")
            lo, hi = s.split("-")
            lo, hi = int(lo.strip()), int(hi.strip())
        except Exception:
            lo, hi = 10, 40
        lo = min(120, max(0, lo))
        hi = min(120, max(lo, hi))
        return lo, hi

    @staticmethod
    def _parse_scenario_map(values):
        """['吃饭=deliver_music,draw_doodle', ...] → {'吃饭': {...工具名}}；空行忽略"""
        out = {}
        for v in values or []:
            s = str(v).strip()
            if "=" not in s:
                continue
            tag, _, names = s.partition("=")
            tag = tag.strip()
            tool_set = {n.strip() for n in names.split(",") if n.strip()}
            if tag:
                out[tag] = tool_set
        return out

    # ---------- 生命周期 ----------

    async def initialize(self):
        try:
            from astrbot.api.star import StarTools

            self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            self._data_dir = Path("data/plugin_data") / PLUGIN_NAME
            self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self._task = asyncio.create_task(self._tick_loop())
        # v2.1.0 一次性心态段生成器：开关只在用户显式打开时为真（生成完自动关回）
        try:
            if bool(self._flatten(self._cfg_obj or {}).get("dm_miss_stance_gen")):
                asyncio.create_task(self._stance_gen_once())
        except Exception:
            pass
        logger.info(
            f"[主动私聊] v{PLUGIN_VERSION} 已启动：目标={self._target_map() or '（未解析到）'} "
            f"晨起={'开' if self.c_morning else '关'} 睡前={'开' if self.c_night else '关'} "
            f"随机想念={self.c_random_n}次/天 上限{self.c_cap}条"
        )

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass  # cancel 的预期结果（CancelledError 不属于 Exception，须单独接住）
            except Exception:
                pass
        self._save_state()

    # ---------- 状态持久化 ----------

    def _st_path(self) -> Path:
        return self._data_dir / "dm_state.json"

    def _load_state(self):
        try:
            self._st = json.loads(
                self._st_path().read_text(encoding="utf-8")
            )
        except Exception:
            self._st = {}
        self._rollover_day()

    def _save_state(self):
        try:
            self._st_path().write_text(
                json.dumps(self._st, ensure_ascii=False, indent=1),
                encoding="utf-8",
                newline="",
            )
        except Exception:
            logger.warning("[主动私聊] 状态落盘失败（不影响运行）")

    def _rollover_day(self):
        """跨天滚动：清计数、掷当日微扰、心情游走、锚点检查；
        timer 模式仍按窗口生成今天的随机想念计划"""
        today = datetime.now().date().isoformat()
        if self._st.get("date") == today:
            # v1.4→v1.5 升级当天：旧版可能已翻过天，日微扰键不存在——只补缺的，不动今日计数
            if "drift" not in self._st:
                if self.c_drift:
                    self._st["drift"] = {
                        "silence": self.c_silence_min, "mid": self.c_mid_min,
                        "afk": self.c_afk_min, "base": self.c_base_p,
                    }
                else:
                    self._st["drift"] = None
            self._st.setdefault("mood", 1.0)  # 中途升级没有昨天的游走值，从1.0起步
            self._st.setdefault("ledger", {"inner": 0, "gen": 0, "tools": 0, "vetoed": 0, "void_gen": 0})
            self._st.setdefault("morning_skip", 0)
            self._st.setdefault("evening_skip", 0)
            self._st.setdefault("night_chat", {})
            return
        yesterday_greeted_m = bool((self._st.get("morning") or {}).get("sent"))
        yesterday_greeted_n = bool((self._st.get("night") or {}).get("sent"))
        self._st["morning_skip"] = 0 if yesterday_greeted_m else int(self._st.get("morning_skip", 0)) + 1
        self._st["evening_skip"] = 0 if yesterday_greeted_n else int(self._st.get("evening_skip", 0)) + 1
        # 每日微扰（±20%，当日固定）：阈值三档 + base_p
        if self.c_drift:
            def _dr(x, lo, hi):
                v = x * random.uniform(1 - DRIFT_STEP, 1 + DRIFT_STEP)
                return min(hi, max(lo, v))
            self._st["drift"] = {
                "silence": _dr(self.c_silence_min, 20, 480),
                "mid": _dr(self.c_mid_min, 15, 240),
                "afk": _dr(self.c_afk_min, 10, 120),
                "base": _dr(self.c_base_p, 0.05, 1.0),
            }
        else:
            self._st["drift"] = None
        # v1.7.0 按人区分的当日计数/每人微扰跨天清零（每人微扰当日懒掷，见 _drift_vals）
        self._st["per"] = {}
        self._st["pdrift"] = {}
        # 心情节律：多日随机游走 ±15%，夹在 [0.85, 1.15]
        mood = float(self._st.get("mood", 1.0)) + random.uniform(-MOOD_STEP, MOOD_STEP)
        self._st["mood"] = round(min(1 + MOOD_STEP, max(1 - MOOD_STEP, mood)), 3)
        # 夜聊频率滑窗：只留最近 8 天
        nc = self._st.get("night_chat") or {}
        keep = sorted(nc.keys())[-8:]
        self._st["night_chat"] = {k: nc[k] for k in keep if nc[k]}
        # 决策日志滚动（最近 400 条）
        if len(self._st.get("dlog") or []) > 400:
            self._st["dlog"] = self._st["dlog"][-400:]
        # token 台账按日清零
        self._st["ledger"] = {"inner": 0, "gen": 0, "tools": 0, "vetoed": 0, "void_gen": 0}
        now = datetime.now()
        lo_dt = now.replace(hour=self.c_win_lo // 60, minute=self.c_win_lo % 60, second=0, microsecond=0)
        hi_dt = now.replace(hour=self.c_win_hi // 60, minute=self.c_win_hi % 60, second=0, microsecond=0)
        plans = []
        if self.c_miss_mode == "timer" and self.c_random_n and lo_dt < hi_dt:
            for _ in range(self.c_random_n):
                at = random.uniform(0, 1)
                ts = (lo_dt + (hi_dt - lo_dt) * at).timestamp()
                if ts > time.time():
                    plans.append(ts)
        self._st.update({
            "date": today,
            "morning": None,   # {"key","at","sent"} 晨起计划
            "night": None,     # {"key","sent"} 睡前（按夜锚日去重）
            "random_plan": sorted(plans),
            "random_sent": 0,
            "sent_total": 0,
            "last_dm_ts": 0.0,
        })
        # 会话状态裁剪：消息流水只留 26h（够覆盖昨夜睡前窗口）
        for s in (self._st.get("sess") or {}).values():
            cut = time.time() - 26 * 3600
            s["msg_times"] = [t for t in (s.get("msg_times") or []) if t >= cut]
        self._save_state()

    # ---------- v1.5 会话状态 ----------

    def _sess(self, umo: str) -> dict:
        """每个目标会话的静默钟/热度/反馈/骰子状态（懒建）"""
        sess = self._st.setdefault("sess", {})
        s = sess.get(umo)
        if s is None:
            s = sess[umo] = {
                "act_ts": 0.0, "act_from": "", "msg_times": [],
                "night_in": {},        # date -> True（睡眠期来言=存稿）
                "reply_factor": 1.0, "pending_reply": None,
                "fires": [],           # 想你触发日期（日期档用，最近30天）
                "unans": 0,            # 未回主动消息计数（熔断）
                "mute_until": 0.0, "mute_openers": 0,
                "afk": None, "promise": None,   # {word?, at, event}
                "dice": None,          # {event, rolled, n/max_n, t0/tau, haz, p, factors, tier, next_at, done}
                "pending": None,       # {event, at} 中签后的抖动定时
            }
        return s

    # ---------- 睡觉插件联动 ----------

    def _sleep_state(self):
        """读睡觉插件状态文件；没有/失败/关闭联动 → None（视为白天清醒）"""
        if not self.c_respect_sleep:
            return None
        for p in SLEEP_STATE_CANDIDATES:
            # 相对 cwd 的候选之外，再试实例数据目录（StarTools 同级）
            for cand in (p, self._data_dir.parent / "astrbot_plugin_dafeiyu_sleep" / "sleep_state.json"):
                try:
                    if cand.exists():
                        return json.loads(cand.read_text(encoding="utf-8"))
                except Exception:
                    continue
        return None

    def _sleep_phase(self) -> str:
        st = self._sleep_state()
        if not st:
            return "awake"  # 无睡觉插件数据 = 不受作息约束
        ph = str(st.get("phase") or "awake")
        return ph if ph in ("awake", "sleeping", "temp_wake") else "awake"

    # ---------- v1.5 被动侧：全消息监听记账（静默钟/热度/反馈/熔断/抑制/存稿） ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """只记账不动消息（任何异常静默放行）。群消息 v1.5 忽略（群信号是 v1.5.1）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":FriendMessage:" not in umo:
                return
            sender = str(event.get_sender_id() or "")
            self_id = ""
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                pass
            now = time.time()
            text = str(getattr(event, "message_str", "") or "")
            if sender and sender == self_id:
                self._stamp(umo, now, "bot", "")   # 自己的普通回复也算"会话有动静"
                return
            self._stamp(umo, now, "user", text)
        except Exception:
            pass  # 记账失败绝不影响消息本身

    def _stamp(self, umo: str, now: float, who: str, text: str):
        if who == "user":
            s = self._sess(umo)
            prev_act = float(s.get("act_ts") or 0)
            opener = who == "user" and (now - prev_act) > 30 * 60  # 隔半小时再开口=开新话题
            s["act_ts"] = now
            s["act_from"] = "user"
            s["msg_times"] = (s.get("msg_times") or []) + [now]
            cut = now - 26 * 3600
            s["msg_times"] = [t for t in s["msg_times"] if t >= cut]
            # 回复反馈懒结算：挂着待观察 → 按延迟给奖励值，Rescorla-Wagner 更新期待值
            pr = s.get("pending_reply")
            if pr:
                latency = now - float(pr.get("sent_at") or now)
                if latency <= self.c_reply_fast * 60:
                    r = self._P(umo).rw_r_fast
                else:
                    r = self._P(umo).rw_r_slow  # 慢回/超慢回同档（真没回由懒结算按 none 档）
                s["pending_reply"] = None
                self._rw_update(umo, r)
                self._dlog({"t": "reply", "latency_min": round(latency / 60, 1), "r": r})
            # 熔断解冻：你说话了
            if s.get("unans"):
                s["unans"] = 0
            # 显式抑制：命中抑制词 → 暂停；开题两次 → 解冻
            muted_now = False
            if self.c_mute_words and any(w in text for w in self.c_mute_words):
                s["mute_until"] = now + self.c_mute_days * 86400
                s["mute_openers"] = 0
                muted_now = True
                self._dlog({"t": "mute_on", "word_seen": True, "until_date": datetime.fromtimestamp(s["mute_until"]).date().isoformat()})
            if (not muted_now) and s.get("mute_until") and opener:
                s["mute_openers"] = int(s.get("mute_openers") or 0) + 1
                if s["mute_openers"] >= 2:
                    s["mute_until"] = 0.0
                    s["mute_openers"] = 0
                    self._dlog({"t": "mute_off", "why": "主动开话2次"})
            # 离席/约定词：挂在当前静默事件上（事件=act_ts 起算的静默段）
            if self.c_afk_words:
                hit = next((w for w in self.c_afk_words if w in text), None)
                if hit:
                    s["afk"] = {"word": hit, "event": now}
            if self.c_promise_words:
                hit = next((w for w in self.c_promise_words if w in text), None)
                if hit:
                    s["promise"] = {"word": hit, "event": now}
            # 你先道了别：记下时刻（起点时刻后算数，防白天误伤）——她睡得再晚，躺下也必收尾
            if self.c_night_words:
                dt = datetime.fromtimestamp(now)
                if dt.hour * 60 + dt.minute >= self.c_night_from and any(w in text for w in self.c_night_words):
                    s["goodnight_ts"] = now
            # 在一起语境：对方说过"一起/连麦…"——窗口内她的主动话术避开"想你/等你"防穿帮
            if self.c_together_words:
                hit = next((w for w in self.c_together_words if w in text), None)
                if hit:
                    s["together_ts"] = now
                    s["together_word"] = hit
            # 存稿：睡眠期来言（自己的记录，不依赖 sleep 缓冲内部结构）
            if self._sleep_phase() == "sleeping":
                s["night_in"][datetime.now().date().isoformat()] = True
            # 夜聊频率：22 点后来的言
            if datetime.now().hour >= 22:
                nc = self._st.setdefault("night_chat", {})
                nc[datetime.now().date().isoformat()] = True
            if s.get("pending") and opener is False:
                pass  # 抖动期你来消息：由评估器按 event 变化作废，这里不处理
        else:
            s = self._sess(umo)
            s["act_ts"] = now
            s["act_from"] = "bot"

    # ---------- v1.5 日历：大小周/节假日/调休 ----------

    def _is_free_day(self, d=None) -> bool:
        d = d or datetime.now().date()
        key_full = d.isoformat()
        key_md = d.strftime("%m-%d")
        if key_full in self.c_busy_dates or key_md in self.c_busy_dates:
            return False  # 调休补班，优先级最高
        if key_full in self.c_free_dates or key_md in self.c_free_dates:
            return True
        if self.c_free_mode == "off":
            return False
        if self.c_free_mode == "pattern":
            if not self.c_free_anchor:
                return d.weekday() in self.c_free_wd  # 没填锚日退化为固定周末
            weeks = (d - self.c_free_anchor).days // 7
            big_week = (weeks % 2) == 0  # 锚日起算：第0/2/4…周=大周
            wd = self.c_free_wd_big if big_week else self.c_free_wd_small
            return d.weekday() in wd
        return d.weekday() in self.c_free_wd

    def _night_owl_level(self) -> str:
        """夜聊频率分档：最近 greet 窗口天数里 22 点后有来言的天数"""
        nc = self._st.get("night_chat") or {}
        days = sum(1 for v in nc.values() if v)
        if days >= 5:
            return "owl"
        if days >= 2:
            return "mid"
        return "early"

    # ---------- v1.5 评估时段：自动跟作息 ----------

    def _quiet_window(self) -> tuple:
        """(lo_min, hi_min) 当日分钟。auto=醒来+1h ~ 今晚入睡-30min（夹硬边界）；
        sleep 读不到→沿用上次推导缓存→再退手动值"""
        if self.c_quiet_mode == "manual":
            return _clamp_lo_hi(self.c_quiet_lo, self.c_quiet_hi)
        st = self._sleep_state()
        if st:
            wake_ts = float(st.get("wake_at") or 0)
            bed_ts = self._night_bed_from_state(st, time.time())
            if wake_ts and bed_ts and bed_ts > wake_ts:
                lo = (datetime.fromtimestamp(wake_ts + 3600))
                hi = (datetime.fromtimestamp(bed_ts - 1800))
                self._quiet_cache = (lo, hi)
                return _clamp_lo_hi(lo.hour * 60 + lo.minute, hi.hour * 60 + hi.minute)
        if self._quiet_cache:
            lo, hi = self._quiet_cache
            return _clamp_lo_hi(lo.hour * 60 + lo.minute, hi.hour * 60 + hi.minute)
        return _clamp_lo_hi(self.c_quiet_lo, self.c_quiet_hi)

    def _in_quiet(self, now: float = None) -> bool:
        lo, hi = self._quiet_window()
        dt = datetime.fromtimestamp(now if now is not None else time.time())
        m = dt.hour * 60 + dt.minute
        return lo <= m <= hi

    # ---------- v1.5 热度：轮数聚合 ----------

    def _heat_rounds(self, umo: str, now: float) -> int:
        """沉默前 heat_window 分钟内你的轮数（burst_gap 聚合）"""
        s = self._sess(umo)
        last = float(s.get("act_ts") or 0)
        if not last:
            return 0
        win_lo = last - self.c_heat_win * 60
        ts = [t for t in (s.get("msg_times") or []) if win_lo <= t <= last]
        if not ts:
            return 0
        rounds, prev = 1, ts[0]
        for t in ts[1:]:
            if t - prev > self.c_burst_gap:
                rounds += 1
            prev = t
        return rounds

    # ---------- v1.5 概率引擎 ----------

    def _date_factor(self, umo: str) -> float:
        """日期冷却（v2.0.0 指数恢复）：f(d)=1−a·e^(−(d−1)/τ)。
        昨天来过折扣最深（f=1−a），之后按恢复常数 τ 指数回血（空闲日 τ×0.5 更快），
        ≥0.97 视为满血——替代 v1.x 的三点折线（0.35/0.65/1.0）"""
        s = self._sess(umo)
        fires = [d for d in (s.get("fires") or []) if d]
        if not fires:
            return 1.0
        try:
            last = datetime.strptime(sorted(fires)[-1], "%Y-%m-%d").date()
        except Exception:
            return 1.0
        d = (datetime.now().date() - last).days
        if d < 1:
            return 1.0
        P = self._P(umo)
        tau = P.cd_tau * (0.5 if self._is_free_day() else 1.0)
        f = 1.0 - P.cd_a * math.exp(-(d - 1) / tau)
        return 1.0 if f >= 0.97 else round(f, 3)

    def _heat_factor(self, rounds: int, umo: str = None) -> float:
        """热度饱和曲线（v2.0.0）：f(r)=1+(hot−1)·r/(K+r)，冷场折扣在前 cold_max 轮线性淡出。
        没有第 9 轮/第 10 轮之间的悬崖，K=半饱和轮数（几轮算热络）；r→∞ 渐近 hot_f"""
        if not self.c_heat_on:
            return 1.0
        P = self._P(umo) if umo else None
        cold_f = P.heat_cold_f if P else self.c_heat_cold_f
        hot_f = P.heat_hot_f if P else self.c_heat_hot_f
        k = P.heat_k if P else self.c_heat_k
        r = max(0, rounds)
        f = 1.0 + (hot_f - 1.0) * r / (k + r)
        if r < self.c_heat_cold_max:
            f -= (1.0 - cold_f) * (1.0 - r / max(1, self.c_heat_cold_max))
        return round(f, 3)

    def _tier_for(self, umo: str, now: float, rounds: int):
        """返回 (档名, 阈值分钟)。bot 自己发起没人回 ×1.5"""
        s = self._sess(umo)
        drift = self._drift_vals(umo)
        P = self._P(umo)
        ev = float(s.get("act_ts") or 0)
        afk = s.get("afk") or {}
        promise = s.get("promise") or {}
        grace = AFK_GRACE_MIN * 60
        # 词的event是"说出词那条消息"的时刻；分开发"吃饭啦""拜拜"时最后一条≠说词条
        # ——距词不超过宽限期仍认（聊超过宽限才安静=人没走，回落普通档）
        if promise and 0 <= ev - float(promise.get("event") or -1) <= grace:
            tier = "promise"   # 约定未赴 → 中档
        elif afk and 0 <= ev - float(afk.get("event") or -1) <= grace:
            tier = "afk"       # 离席词 → 吃饭档
        elif rounds >= self.c_heat_hot_min and s.get("act_from") == "user":
            tier = "hot"       # 聊嗨断线无交代 → 中档
        else:
            tier = "normal"
        base = {"afk": drift.get("afk", P.afk_min),
                "promise": drift.get("mid", P.mid_min),
                "hot": drift.get("mid", P.mid_min),
                "normal": drift.get("silence", P.silence_min)}[tier]
        if s.get("act_from") == "bot":  # 机器人自己发起没人回：×1.5
            base = base * 1.5
        if self._is_free_day():  # v2.1.0 空闲日：没聊=真没聊，起评线提前
            base = base * self.c_free_thr_mult
        return tier, float(base)

    def _miss_probability(self, umo: str, tier: str, rounds: int) -> tuple:
        """四因子连乘→总下限→心情。返回 (p, 因子明细)"""
        s = self._sess(umo)
        P = self._P(umo)
        drift = self._drift_vals(umo)
        base = float(drift.get("base", P.base_p))
        # v2.1.0 空闲日档：周末/节假日基础概率上浮（消息发起率的日历效应；
        # 频率仍被每日上限/硬顶/间隔夹死——概率放开、频率夹死不变）
        f_free = round(self.c_free_p_mult, 2) if self._is_free_day() else 1.0
        if f_free != 1.0:
            base = min(1.0, base * f_free)
        f_date = self._date_factor(umo)
        f_reply = self._rw_factor(umo)
        f_heat = self._heat_factor(rounds, umo)
        # 吃饭档折扣随热度波动（v1.5.9）：饭前聊得越热折扣池越高，每次饭点在池内随机落点
        # ——聊嗨了去吃饭她坐不住（0.7~1.0），平平常常吃饭矜持（0.4~0.7），冷场散伙不来（0.1~0.4）
        f_afk = 1.0
        if tier == "afk":
            if rounds >= self.c_heat_hot_min:
                lo, hi = 0.7, 1.0
            elif rounds <= self.c_heat_cold_max:
                lo, hi = 0.1, 0.4
            else:
                lo, hi = 0.4, 0.7
            f_afk = round(random.uniform(lo, hi), 3)
        p = base * f_date * f_reply * f_heat * f_afk
        p = max(P.floor, p)
        p = p * float(self._st.get("mood", 1.0))
        p = min(1.0, p)
        factors = {"base": round(base, 3), "date": f_date, "reply": round(f_reply, 2),
                   "heat": round(f_heat, 2), "afk": f_afk, "floor": P.floor,
                   "mood": float(self._st.get("mood", 1.0))}
        if f_free != 1.0:
            factors["free"] = f_free
        return p, factors

    def _together_now(self, umo: str, now: float) -> tuple:
        """(是否处于'在一起'语境, 命中词, 已过分钟)。词表清空=功能关。"""
        s = self._sess(umo)
        ts = float(s.get("together_ts") or 0)
        if not ts or not self.c_together_words:
            return False, "", 0
        elapsed = now - ts
        if 0 <= elapsed <= self.c_together_min * 60:
            return True, str(s.get("together_word") or ""), int(elapsed // 60)
        return False, "", 0

    def _together_ctx(self, umo: str, now: float) -> str:
        """在一起语境的事实行（喂内心戏与正文，话术防穿帮；不动概率）"""
        tg, word, mins = self._together_now(umo, now)
        if not tg:
            return ""
        return (
            f"[背景事实：约{mins}分钟前对方说过「{word}」——你们可能正连着麦/一起玩/待在一块。"
            f"别说「想你」「等你回来」这种分开才说的话；像陪在旁边那样随口聊眼前的事就行。]\n"
        )

    def _scenario_tag(self, umo: str, tier: str) -> str:
        s = self._sess(umo)
        tg, _, _ = self._together_now(umo, time.time())
        if tg:
            return "在一起"
        days = 0
        fires = [d for d in (s.get("fires") or []) if d]
        if fires:
            try:
                days = (datetime.now().date() - datetime.strptime(sorted(fires)[-1], "%Y-%m-%d").date()).days
            except Exception:
                days = 0
        if tier == "afk":
            return "吃饭"
        if tier in ("promise", "hot"):
            return "聊嗨断线" if tier == "hot" else "等约"
        if self._rw_factor(umo) < 1.0 and s.get("pending_reply") is None and days == 0:
            return "被晾"
        if days >= 3:
            return "久别"
        return "普通"

    # ---------- v1.5 决策日志与 token 台账 ----------

    def _dice_done(self, umo: str):
        """终结本静默事件的骰子（中签/否决/作废后不再重想）"""
        d = self._sess(umo).get("dice")
        if d:
            d["done"] = True
            d["next_at"] = 0.0

    def _dlog(self, entry: dict):
        entry["ts"] = datetime.now().strftime("%m-%d %H:%M")
        log = self._st.setdefault("dlog", [])
        log.append(entry)
        if len(log) > 400:
            del log[: len(log) - 400]

    def _ledger_add(self, key: str, n: int = 1):
        led = self._st.setdefault("ledger", {})
        led[key] = int(led.get(key) or 0) + n

    # ---------- 目标与平台 ----------

    def _platform_ids(self) -> list:
        """全部可用平台 ID。配置指定了 dm_platform 就只用它。
        注：get_insts() 返回列表（部分旧版是 dict），平台 ID 在 config["id"] 或 meta().id"""
        if self.c_platform:
            return [self.c_platform]
        pids = []
        try:
            insts = self.context.platform_manager.get_insts()
            if isinstance(insts, dict):  # 兼容旧版
                insts = insts.values()
            for inst in insts:
                try:
                    pid = getattr(inst.meta(), "id", None) or (inst.config or {}).get("id")
                except Exception:
                    pid = (getattr(inst, "config", None) or {}).get("id")
                pid = str(pid) if pid else None
                if pid and pid not in pids and pid != "webchat":
                    pids.append(pid)
        except Exception:
            pass
        return pids

    def _target_map(self) -> dict:
        """{platform_id: [qq...]}。显式填了 dm_targets → 目标放首个平台（沿用旧行为）；
        留空 → ①按平台各自解析绑定配置的管理员 ②再并集所有配置文件的 admins_id——
        多配置文件常绑定到具体会话（平台级探测拿不到），同一 bot 服务的所有配置的
        管理员都视作它的主人，并集兜底挂在第一个平台下发。"""
        if self.c_targets:
            pids = self._platform_ids()
            return {pids[0]: list(self.c_targets)} if pids else {}
        result = {}
        for pid in self._platform_ids():
            try:
                cfg = self.context.get_config(umo=f"{pid}:FriendMessage:0")
                admins = cfg.get("admins_id", []) or []
            except Exception:
                try:
                    admins = self.context.get_config().get("admins_id", []) or []
                except Exception:
                    admins = []
            qs = list(dict.fromkeys(str(a).strip() for a in admins if str(a).strip()))
            if qs:
                result[pid] = qs  # 平台内去重；跨平台同名QQ不去重（各bot发各的管理员）
        # 并集补充：全部配置文件的管理员（绑定到具体会话的配置在这里才被看到）
        pids = self._platform_ids()
        first = pids[0] if pids else None
        if first is not None:
            try:
                confs = getattr(self.context.astrbot_config_mgr, "confs", {}) or {}
            except Exception:
                confs = {}
            for conf in (confs.values() if hasattr(confs, "values") else []):
                try:
                    for a in conf.get("admins_id", []) or []:
                        a = str(a).strip()
                        if not a or any(a in qs for qs in result.values()):
                            continue
                        result.setdefault(first, [])
                        if a not in result[first]:
                            result[first].append(a)
                except Exception:
                    continue
        return result

    def _targets(self):
        flat = []
        for qs in self._target_map().values():
            for q in qs:
                if q not in flat:
                    flat.append(q)
        return flat

    # ---------- 主循环 ----------

    async def _tick_loop(self):
        while True:
            try:
                await self._on_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[主动私聊] tick 异常（跳过本轮）", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)

    async def _on_tick(self):
        if not self.enabled:
            return
        # 补发通道：管理员显式意志，跳过活跃终检强制发（仍计总量）
        mk = self._consume_makeup()
        if mk:
            tpl = {
                "night": self.tpl_makeup_night,
                "morning": self.tpl_morning,
                "random": self.tpl_random,
            }.get(mk, self.tpl_random)
            await self._fire("makeup", tpl)
        self._rollover_day()
        now = time.time()
        phase = self._sleep_phase()
        sl = self._sleep_state() or {}

        # ---------- 晨起 ----------
        if self.c_morning:
            wake_ts = float(sl.get("wake_at") or 0)
            m = self._st.get("morning")
            if wake_ts and now - wake_ts < 6 * 3600:
                key = datetime.fromtimestamp(wake_ts).date().isoformat()
                if not m or m.get("key") != key:
                    shift = random.uniform(30, 60) * 60 if self._night_owl_level() == "owl" else 0
                    m = {"key": key, "at": wake_ts + random.uniform(5, 30) * 60 + shift, "sent": False}
                    self._st["morning"] = m
                    self._save_state()
            if m and not m.get("sent") and now >= m.get("at", 0):
                if phase == "temp_wake":
                    m["at"] = now + 10 * 60
                    self._save_state()
                elif phase == "awake":
                    m["tries"] = int(m.get("tries", 0)) + 1
                    await self._morning_decide(m, wake_ts, now)
                    self._save_state()

        # ---------- 睡前 ----------
        if self.c_night:
            bed_ts = self._tonight_bed_ts(sl, now)
            if bed_ts:
                key = datetime.fromtimestamp(bed_ts).date().isoformat()
                n = self._st.get("night")
                if not n or n.get("key") != key:
                    n = {"key": key, "sent": False}
                    self._st["night"] = n
                    self._save_state()
                if not n.get("sent") and phase == "awake" and 0 < bed_ts - now <= NIGHT_WINDOW_MIN * 60 + 60:
                    n["tries"] = int(n.get("tries", 0)) + 1
                    await self._night_decide(n, bed_ts, now)
                    self._save_state()

        # ---------- 随机想念：timer 模式沿用 v1.4（到点即发+按住队列） ----------
        if self.c_miss_mode == "timer":
            plans = self._st.get("random_plan") or []
            due = [t for t in plans if t <= now]
            if due:
                rest = [t for t in plans if t > now]
                for ts in due:
                    if phase == "awake" and self._quota_ok(now):
                        if await self._fire("random", self.tpl_random):
                            self._st["random_sent"] = int(self._st.get("random_sent", 0)) + 1
                    elif phase == "sleeping" or phase == "temp_wake":
                        late = now + 30 * 60
                        end_dt = datetime.now().replace(
                            hour=self.c_win_hi // 60, minute=self.c_win_hi % 60, second=0, microsecond=0
                        )
                        if late < end_dt.timestamp():
                            rest.append(late)
                self._st["random_plan"] = sorted(rest)
                self._save_state()
            defer = self._st.get("random_defer") or {}
            if defer:
                end_dt = datetime.now().replace(
                    hour=self.c_win_hi // 60, minute=self.c_win_hi % 60, second=0, microsecond=0
                )
                for k in list(defer):
                    v = defer[k]
                    if now >= end_dt.timestamp() or now - float(v.get("first") or now) >= 3 * 3600:
                        defer.pop(k)
                        continue
                    if float(v.get("at", 0)) > now:
                        continue
                    if phase != "awake" or not self._quota_ok(now):
                        continue
                    umo = f"{v['pid']}:FriendMessage:{v['qq']}"
                    if await self._is_chatting(umo, now):
                        v["at"] = self._roll_defer_at(now)
                        continue
                    if await self._fire("random", self.tpl_random, only_qq=v["qq"]):
                        self._st["random_sent"] = int(self._st.get("random_sent", 0)) + 1
                        defer.pop(k)
                    else:
                        v["tries"] = int(v.get("tries") or 0) + 1
                        if v["tries"] >= 3:
                            defer.pop(k)
                        else:
                            v["at"] = self._roll_defer_at(now)
                self._st["random_defer"] = defer
                self._save_state()

        # ---------- 随机想念：silence 模式（v1.5 主菜） ----------
        elif self.c_miss_mode == "silence":
            await self._silence_eval(now, phase)

        # ---------- 懒结算：待观察超 12h 按没回处理 ----------
        for umo, s in (self._st.get("sess") or {}).items():
            pr = s.get("pending_reply")
            if pr and now - float(pr.get("sent_at") or 0) > self.c_reply_slow * 60:
                s["pending_reply"] = None
                self._rw_update(umo, self._P(umo).rw_r_none)
        self._save_state()

    def _night_bed_from_state(self, st: dict, now: float) -> float:
        """从睡觉插件状态取"当前这一夜的入睡点"。night.bed_ts 落在[现在-16h, 现在+18h]
        即采信——sleep 的 anchor 是傍晚日期，床点跨午夜后（如 02:16）anchor≠"今天"，
        拿日期相等判会把跨午夜晚的床点读丢（v1.5.6 修复：睡前晚安/评估时段跨午夜灭）；
        采信不了再用未来的 sleep_window[0]，都没有返回 0"""
        night = st.get("night") or {}
        b = float(night.get("bed_ts") or 0)
        if b and now - 16 * 3600 <= b <= now + 18 * 3600:
            return b
        sw = st.get("sleep_window") or []
        if sw and float(sw[0]) > now:
            return float(sw[0])
        return 0.0

    def _tonight_bed_ts(self, sl: dict, now: float) -> float:
        """今晚入睡点（判定见 _night_bed_from_state）"""
        return self._night_bed_from_state(sl, now)

    async def _morning_decide(self, m: dict, wake_ts: float, now: float):
        """晨起决策：存稿→让位冲刷；用户先开口→跳过；锚点必发；概率分支掷骰"""
        tmap = self._target_map()
        if not tmap:
            return
        pid, qq = next(iter(tmap.items()))[0], next(iter(tmap.items()))[1][0]
        umo = f"{pid}:FriendMessage:{qq}"
        s = self._sess(umo)
        # 存稿/冲刷让位：睡眠期来言被睡觉插件守门截停，dm 自己看不见（v1.5.7 修复）——
        # 直接读睡觉插件的"晨间冲刷已发生"标记；夜间来言本会触发冲刷，晨间内容已带出，不重复报到
        night_in = s.get("night_in") or {}
        mf = (self._sleep_state() or {}).get("morning_flushed") or {}
        flushed = mf.get("date") == datetime.now().date().isoformat() and umo in (mf.get("umos") or [])
        if night_in or flushed:
            m["sent"] = True
            if night_in:
                s["night_in"] = {}
            self._dlog({"t": "morning_skip", "why": "存稿在,让位晨间冲刷" if night_in else "晨间冲刷已发生,让位"})
            return
        # 用户醒后已先开口 → 冲刷已带出晨间内容
        if wake_ts and await self._user_spoke_after(umo, wake_ts):
            m["sent"] = True
            self._dlog({"t": "morning_skip", "why": "用户已先开口"})
            return
        anchor = int(self._st.get("morning_skip", 0)) >= 2  # 连跳2天→今日必发
        if not m.get("rolled"):
            sl = self._sleep_state() or {}
            ln = sl.get("last_night") or sl.get("night") or {}
            bed_ts = float(ln.get("bed_ts") or 0)
            presleep = 0
            if bed_ts:
                presleep = len([t for t in (s.get("msg_times") or []) if bed_ts - 3600 <= t <= bed_ts])
            f = 1.2 if presleep >= 10 else (0.75 if presleep < 3 else 1.0)
            # 早安版道别对等（v1.5.7）：你当晚道过晚安（同晚8h内）→ 取消"睡前没聊"折扣——
            # 你早睡早道别不该因她睡得晚（睡前一小时你已睡着）被降早安概率
            gn = float(s.get("goodnight_ts") or 0)
            said_bye = bool(bed_ts and bed_ts - 8 * 3600 <= gn <= bed_ts + 3600)
            if said_bye and f < 1.0:
                f = 1.0
            p = self._P(umo).morning_base * f
            m["rolled"] = True
            m["p"] = round(p, 3)
            m["win"] = anchor or (random.random() < p)
            self._dlog({"t": "morning_roll", "p": round(p, 3), "presleep": presleep,
                        "said_bye": said_bye, "anchor": anchor, "win": m["win"]})
        if m.get("win"):
            sent = await self._send_greet("morning", umo, self.tpl_morning, now, force=anchor)
            if sent or m["tries"] >= 6:
                m["sent"] = True
                if sent:
                    self._st["morning_skip"] = 0
            else:
                m["at"] = now + 5 * 60  # 终检/近距查重顺延，5分钟后再看
        else:
            m["sent"] = True  # 没中：今日作废（锚点已强制 win=True）

    async def _night_decide(self, n: dict, bed_ts: float, now: float):
        """睡前决策：今晚聊过→必收尾；没聊→概率×夜聊档；锚点必发；已道晚安→换收尾模板"""
        tmap = self._target_map()
        if not tmap:
            return
        pid, qq = next(iter(tmap.items()))[0], next(iter(tmap.items()))[1][0]
        umo = f"{pid}:FriendMessage:{qq}"
        s = self._sess(umo)
        chatted = len([t for t in (s.get("msg_times") or []) if bed_ts - 2 * 3600 <= t <= now]) > 0
        # 你先道过别（同晚8小时内）→ 即使在她睡前2小时窗口外，躺下也必收尾：
        # 道别是对等的，不该因为她自己睡晚了就漏掉你说的晚安
        gn = float(s.get("goodnight_ts") or 0)
        said_bye = bed_ts - 8 * 3600 <= gn <= now
        if said_bye:
            chatted = True
        already = await self._already_said_goodnight(umo)
        anchor = int(self._st.get("evening_skip", 0)) >= 2
        if not n.get("rolled"):
            owl_f = {"owl": 1.2, "mid": 1.0, "early": 0.7}[self._night_owl_level()]
            p = self._P(umo).evening_base * owl_f
            n["rolled"] = True
            n["p"] = round(p, 3)
            n["win"] = chatted or anchor or (random.random() < p)
            self._dlog({"t": "night_roll", "p": round(p, 3), "chatted": chatted, "said_bye": said_bye,
                        "owl_f": owl_f, "anchor": anchor, "win": n["win"]})
        if n.get("win"):
            tpl = self.tpl_night2 if already else self.tpl_night
            force_at_bed = bed_ts - now <= 60 * 60  # 到睡觉线1小时内：强制发（活跃终检/查重豁免）
            sent = await self._send_greet("night", umo, tpl, now, force=force_at_bed)
            if sent or n["tries"] >= 6:
                n["sent"] = True
                if sent:
                    self._st["evening_skip"] = 0
            else:
                n["tries2"] = int(n.get("tries2") or 0) + 1
                n["at_hint"] = now + 5 * 60  # 顺延5分钟；窗口逻辑下轮 tick 自然再看
        else:
            n["sent"] = True

    async def _send_greet(self, kind: str, umo: str, template: str, now: float, force: bool = False) -> bool:
        """早晚安发送：活跃终检（force=到睡觉线豁免）→ 近距查重 → 否决/生成 → 拟人发送+记账"""
        s = self._sess(umo)
        if not force:
            if self.c_active_check and now - float(s.get("act_ts") or 0) < self.c_active_check * 60:
                return False  # 你正在聊：顺延
            if self.c_greet_gap and now - float(self._st.get("last_dm_ts") or 0) < self.c_greet_gap * 60:
                return False  # 刚发过别的主动：近距查重
        text = await self._gen(template, umo=umo, kind=kind, veto_ok=self.c_ai_veto,
                               veto_ctx=self._status_ctx(umo) + self._stance_line(umo))
        if text is None:
            self._dlog({"t": f"{kind}_veto", "why": "AI否决"})
            return True  # 否决=今日销账（豁免线走不到这，这里只是概率分支被拦）
        try:
            await self._send_human(umo, text)
        except Exception:
            logger.warning(f"[主动私聊] {kind} 发送失败 {umo}", exc_info=True)
            return False
        archive_text = text + (f"\n[本轮工具：{'；'.join(self._tool_trace[:3])}]" if self._tool_trace else "")
        await self._archive_dm(umo, archive_text)
        self._bookkeep(umo, now, kind)
        self._dlog({"t": f"{kind}_sent", "text": text[:30]})
        return True

    def _bookkeep(self, umo: str, now: float, kind: str):
        """发出后的统一记账：总量/间隔/熔断计数/待观察"""
        self._st["sent_total"] = int(self._st.get("sent_total", 0)) + 1
        self._st["last_dm_ts"] = now
        if self.c_tier_per_conf:  # 按人区分开着：每人自己的当日总量/想你想念计数
            qq = str(umo.split(":")[-1])
            d = self._st.setdefault("per", {}).setdefault(qq, {})
            d["sent"] = int(d.get("sent", 0)) + 1
            if kind == "random":
                d["random"] = int(d.get("random", 0)) + 1
        s = self._sess(umo)
        s["unans"] = int(s.get("unans") or 0) + 1
        if kind == "random":
            s["fires"] = (s.get("fires") or [])[-30:] + [datetime.now().date().isoformat()]
            s["pending_reply"] = {"sent_at": now, "kind": kind}

    def _status_ctx(self, umo: str) -> str:
        """喂给内心戏/正文/否决的事实块（v2.1.0 AI 感官包）：时间感官（多久没聊/
        今天休不休息/几点）+ 离席·约定锚点 + 热度画像——模型不再靠猜"现在是什么情况"，
        "拿几小时前的离席当正在离席"这类过期状态误判从根上断掉"""
        s = self._sess(umo)
        now = time.time()
        fires = [d for d in (s.get("fires") or []) if d]
        days_line = "距你上次主动找TA 已隔很久（没有近期记录）"
        if fires:
            try:
                dd = (datetime.now().date() - datetime.strptime(sorted(fires)[-1], "%Y-%m-%d").date()).days
                days_line = f"距你上次主动找TA {dd} 天"
            except Exception:
                pass
        parts = []
        wk = "周" + "一二三四五六日"[datetime.now().weekday()]
        parts.append(f"今天是{wk}（{'休息日' if self._is_free_day() else '工作日'}）")
        act = float(s.get("act_ts") or 0)
        if act:
            mins = max(0, int((now - act) // 60))
            gap = f"{mins // 60} 小时 {mins % 60} 分钟" if mins >= 60 else f"{mins} 分钟"
            anchor = ""
            afk = s.get("afk") or {}
            if isinstance(afk, dict) and afk.get("word"):
                anchor = "，TA {} 说「{}」离开".format(
                    datetime.fromtimestamp(float(afk.get("event") or act)).strftime("%H:%M"), afk["word"])
            promise = s.get("promise") or {}
            if isinstance(promise, dict) and promise.get("word"):
                anchor += "，约过 {}「{}」还没兑现".format(
                    datetime.fromtimestamp(float(promise.get("event") or act)).strftime("%H:%M"), promise["word"])
            parts.append(f"距TA最后一条消息已 {gap}{anchor}")
        rounds = self._heat_rounds(umo, now)
        span = self._heat_span(umo, now)
        if rounds:
            hot = "、聊得很黏" if rounds >= self.c_heat_hot_min else ""
            parts.append(f"沉默前 {span} 分钟里TA来了 {rounds} 轮{hot}" if span else f"窗口内TA来了 {rounds} 轮{hot}")
        v = float(s.get("exp_v", 0.6))
        parts.append(days_line + f"；TA近来的回应热情（期待值 {v:.2f}，乘子 {self._rw_factor(umo):.2f}）")
        parts.append(f"今日已主动 {int(self._st.get('sent_total', 0))} 条；现在 {datetime.now().strftime('%H:%M')}")
        return "[背景事实：" + "；".join(parts) + "]\n"

    def _heat_span(self, umo: str, now: float) -> int:
        """热度窗口内首末消息的跨度（分钟）——画像行"聊了多久"用"""
        s = self._sess(umo)
        last = float(s.get("act_ts") or 0)
        if not last:
            return 0
        win_lo = last - self.c_heat_win * 60
        ts = [t for t in (s.get("msg_times") or []) if win_lo <= t <= last]
        return int((ts[-1] - ts[0]) // 60) if len(ts) >= 2 else 0

    def _stance_line(self, umo: str) -> str:
        """判断心态段（v2.1.0）：QQ 映射 > 配置文件映射(含前缀/default) > 全局心态键。
        只写判断先验不写说话风格——人设管"你是谁"，这段管"判断时往哪边想"。
        空=功能关（用户清空即关）。"""
        txt = ""
        if self._stance_map:
            qq = str(umo.split(":")[-1])
            txt = self._stance_map.get("qq:" + qq) or ""
            if not txt:
                cid = self._conf_of(umo)
                if cid:
                    txt = self._stance_map.get("conf:" + cid.lower()) or ""
                    if not txt:
                        for k, v in self._stance_map.items():
                            if k.startswith("conf:") and cid.lower().startswith(k[5:]):
                                txt = v
                                break
            if not txt:
                txt = self._stance_map.get("conf:default") or ""
        if not txt:
            txt = self.c_stance_hint
        txt = txt.strip()
        return f"[你的判断心态：{txt.replace('{who}', 'TA')}]\n" if txt else ""

    def _cfg_write(self, key: str, value) -> bool:
        """写回配置对象（先找所在分组，找不到落顶层；_flatten 两种都读得到）"""
        cfg = self._cfg_obj
        if not isinstance(cfg, dict):
            return False
        for k, v in cfg.items():
            if isinstance(v, dict) and key in v:
                v[key] = value
                return True
        cfg[key] = value
        return True

    async def _stance_gen_once(self):
        """一次性心态段生成器（v2.1.0）：按当前默认人设写一段判断心态→写回配置→开关自动关回。
        只在用户显式打开 dm_miss_stance_gen 时花这一次 token；任何失败开关保持开（下次重载重试）。
        生成的是全局心态键；按配置/QQ 分流的条目请手填 miss_stance_conf_map。"""
        try:
            try:
                provider = await self.context.get_using_provider_async(None)
            except Exception:
                provider = self.context.get_using_provider()
            if provider is None:
                logger.warning("[主动私聊] 心态段生成：无可用 LLM，开关保持开，下次重载重试")
                return
            persona_text = ""
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(None)
                if isinstance(persona, dict):
                    persona_text = str(persona.get("prompt") or persona.get("system_prompt") or "")
                elif persona is not None:
                    persona_text = str(getattr(persona, "prompt", "") or getattr(persona, "system_prompt", ""))
            except Exception:
                persona_text = ""
            if not persona_text:
                logger.warning("[主动私聊] 心态段生成：取不到默认人设，开关保持开，下次重载重试")
                return
            prompt = (
                "以下是一个聊天机器人的人设：\n" + persona_text[:2000]
                + "\n\n请用第一人称为这个角色写一段「主动发消息前的判断心态」："
                "它主动找人之前心里是怎么想的——想不想找、什么情况真的会按住、"
                "拿不准时往哪边倒。只写判断时的心态倾向，不规定说话风格、不抄人设原文，"
                "50~120字，口吻与人设一致。直接输出这段话本身，不要任何前后缀。"
            )
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, session_id=f"{PLUGIN_NAME}_stance"), timeout=30)
            text = str(getattr(resp, "completion_text", "") or "").strip().strip('"“”')
            if not text:
                logger.warning("[主动私聊] 心态段生成：模型没给内容，开关保持开，下次重载重试")
                return
            text = text[:300]
            self._cfg_write("dm_miss_stance_hint", text)
            self._cfg_write("dm_miss_stance_gen", False)
            try:
                await self._cfg_obj.save_config_async()
            except Exception:
                try:
                    self._cfg_obj.save_config()
                except Exception:
                    logger.warning("[主动私聊] 心态段已生成但配置落盘失败（本次运行内仍生效）", exc_info=True)
            self.c_stance_hint = text
            logger.info(f"[主动私聊] 心态段已按人设生成并写回（{len(text)}字），开关已自动关回：{text[:60]}")
        except Exception:
            logger.warning("[主动私聊] 心态段生成失败，开关保持开，下次重载重试", exc_info=True)

    # ---------- v1.5 静默评估器 ----------

    async def _silence_eval(self, now: float, phase: str):
        tmap = self._target_map()
        if not tmap:
            return
        pid = next(iter(tmap.keys()))
        for qq in tmap[pid]:
            umo = f"{pid}:FriendMessage:{qq}"
            s = self._sess(umo)
            ev = float(s.get("act_ts") or 0)
            if not ev:
                continue  # 从没说过话：无静默可言
            # ① 中签 pending 的到点处理
            pend = s.get("pending")
            if pend and now >= float(pend.get("at") or 0):
                ok, why = self._pending_gates(umo, now, phase)
                if ok:
                    await self._fire_miss(umo, now)
                else:
                    self._dlog({"t": "miss_void", "why": why, "umo": qq})
                    s["pending"] = None
                    self._dice_done(umo)  # 作废即终结，不再重想
                continue
            if pend:
                continue  # 还没到抖动时刻
            # ② 掷骰评估
            if phase != "awake" or not self._in_quiet(now):
                continue
            if s.get("mute_until") and now < s.get("mute_until"):
                continue
            if int(s.get("unans") or 0) >= self.c_breaker:
                continue  # 熔断中
            P = self._P(umo)
            if self._per_cnt(umo, "random") >= P.daily_max:
                continue
            if not self._quota_ok(now, umo):
                continue
            silence = now - ev
            if silence > 18 * 3600:
                d = s.get("dice")
                if not d or float(d.get("event") or 0) != ev:
                    s["dice"] = {"event": ev, "rolled": True, "result": "over18h"}
                continue  # 跨夜长冷场交给晨起
            rounds = self._heat_rounds(umo, now)
            tier, base_thr = self._tier_for(umo, now, rounds)
            d = s.get("dice")
            attempt = 1
            if d and float(d.get("event") or 0) == ev and d.get("rolled"):
                # v1.5.10 长冷场重想：次数没耗尽且到点才衰减重掷；中签/否决/作废一次即终结
                if d.get("done") or int(d.get("n") or 1) >= int(d.get("max_n") or 1):
                    continue
                if now < float(d.get("next_at") or 0):
                    continue
                attempt = int(d.get("n") or 1) + 1
            # v2.0.0 Weibull 触发：风险随静默时长连续上升（尺度=旧阈值×性格乘子×当日微扰），
            # 本 tick 以 p_haz 掷"想起你"——替代"静默满 N 分钟才开始评估"的阶跃阈值
            T_eff = base_thr * 60.0 * P.haz_scale
            p_haz = self._haz_p(silence, T_eff)
            if random.random() >= p_haz:
                continue
            p, factors = self._miss_probability(umo, tier, rounds)
            if attempt == 1:
                # 本事件总次数随热度抽（聊得越热她想起你的次数越多；抽的，观察不出固定数）
                if rounds >= self.c_heat_hot_min:
                    max_n = random.randint(2, 3)
                elif rounds <= self.c_heat_cold_max:
                    max_n = 1
                else:
                    max_n = random.randint(1, 2)
                # 想念半衰期随热度伸缩（v1.6.1，池内随机抽防可数）：聊得越热念想褪得越慢
                if rounds >= self.c_heat_hot_min:
                    hm = random.uniform(1.3, 1.6)
                elif rounds <= self.c_heat_cold_max:
                    hm = random.uniform(0.6, 0.8)
                else:
                    hm = random.uniform(0.9, 1.1)
                tau_eff = P.retry_tau * hm
                t0 = now
            else:
                max_n = int(d.get("max_n") or 1)
                tau_eff = float(d.get("tau") or P.retry_tau)
                t0 = float(d.get("t0") or now)
                # 重想衰减：双曲想念曲线 U=U0/(1+t/τ)——前快后慢带长尾（Mazur 双曲贴现在人头对头
                # 比较里赢指数模型；ACT-R："还会被想起"的概率随时间幂律下降）。τ 第一次掷时就抽好。
                t_h = max(0.0, (now - t0) / 3600.0)
                f_retry = 1.0 / (1.0 + t_h / tau_eff)
                p = p * f_retry
                factors = dict(factors)
                factors["retry"] = round(f_retry, 3)
            s["dice"] = {"event": ev, "rolled": True, "n": attempt, "max_n": max_n,
                         "t0": t0, "tau": round(tau_eff, 3),
                         "haz": round(p_haz, 4), "p": round(p, 3),
                         "factors": factors, "tier": tier,
                         "next_at": (now + random.uniform(P.retry_gap_lo, P.retry_gap_hi) * 3600)
                         if attempt < max_n else 0.0}
            if random.random() < p:
                lo, hi = self.c_jitter_lo, self.c_jitter_hi
                s["pending"] = {"event": ev, "at": now + random.uniform(lo, hi) * 60}
                self._dice_done(umo)
                self._dlog({"t": "miss_win", "umo": qq, "tier": tier, "p": round(p, 3),
                            "rounds": rounds, "factors": factors, "n": attempt,
                            "at": datetime.fromtimestamp(s["pending"]["at"]).strftime("%H:%M")})
            else:
                self._dlog({"t": "miss_lose", "umo": qq, "tier": tier, "p": round(p, 3),
                            "rounds": rounds, "n": attempt})
        self._save_state()

    def _pending_gates(self, umo: str, now: float, phase: str):
        """中签到点的终检。返回 (通过, 未过原因)"""
        s = self._sess(umo)
        if float(s.get("pending", {}).get("event") or 0) != float(s.get("act_ts") or -1):
            return False, "事件翻篇(你回来过)"  # 含活跃终检语义：任何新消息都换事件
        if phase != "awake":
            return False, "睡着了"
        if not self._in_quiet(now):
            return False, "过评估时段"
        if s.get("mute_until") and now < s.get("mute_until"):
            return False, "显式抑制中"
        if int(s.get("unans") or 0) >= self.c_breaker:
            return False, "熔断中"
        if self._per_cnt(umo, "random") >= self._P(umo).daily_max:
            return False, "今日上限"
        if not self._quota_ok(now, umo):
            return False, "频控(间隔/总量)"
        return True, ""

    async def _fire_miss(self, umo: str, now: float):
        """中签发送：否决（可选）→ 情景门控工具生成 → 拟人发送 → 记账"""
        s = self._sess(umo)
        rounds = self._heat_rounds(umo, now)
        tier = (s.get("dice") or {}).get("tier") or "normal"
        tag = self._scenario_tag(umo, tier)
        if self.c_gen_mode.get("random") == "template":
            text = random.choice(self.c_pool)
            status = "ok"
        else:
            text = await self._gen(
                self.tpl_miss_random, umo=umo, kind="random",
                veto_ok=self.c_ai_veto,
                veto_ctx=self._status_ctx(umo) + self._together_ctx(umo, now)
                    + self._stance_line(umo)
                    + f"[本次情景：{tag}（{'去吃饭离席' if tier=='afk' else '约定未赴' if tier=='promise' else '聊嗨断线' if tier=='hot' else '普通冷场'}）]\n",
                scenario=tag,
            )
            status = self._last_gen_status
        s["pending"] = None
        if text is None:
            self._ledger_add("vetoed")
            why = getattr(self, "_last_veto_reason", "")
            self._dlog({"t": "miss_veto", "umo": umo.split(":")[-1], "tag": tag, "why": why[:60]})
            self._dice_done(umo)  # 否决=今日销账，不再重想
            return  # 否决：不耗每日额度，骰子已记已用
        try:
            await self._send_human(umo, text)
        except Exception:
            logger.warning(f"[主动私聊] 想你发送失败 {umo}", exc_info=True)
            return
        archive_text = text + (f"\n[本轮工具：{'；'.join(self._tool_trace[:3])}]" if self._tool_trace else "")
        await self._archive_dm(umo, archive_text)
        self._st["random_sent"] = int(self._st.get("random_sent", 0)) + 1
        self._bookkeep(umo, now, "random")
        self._dlog({"t": "miss_sent", "umo": umo.split(":")[-1], "tag": tag, "text": text[:30]})

    def _quota_ok(self, now, umo: str = None) -> bool:
        """频控：总量/间隔。按人区分开着且有 umo → 每人自己的总量上限
        （全局硬顶 ABS_TOTAL_CAP 与两次最短间隔是账户级保险丝，照旧全局管）"""
        if umo and self.c_tier_per_conf:
            if int(self._st.get("sent_total", 0)) >= ABS_TOTAL_CAP:
                return False
            if self._per_cnt(umo, "sent") >= self._P(umo).cap:
                return False
        else:
            if int(self._st.get("sent_total", 0)) >= self.c_cap:
                return False
        if now - float(self._st.get("last_dm_ts", 0)) < self.c_gap * 3600:
            return False
        return True

    async def _archive_dm(self, umo: str, text: str):
        """把发出的主动消息归档进该会话的对话历史（user 侧只放极简标记）。
        不归档的话，主对话上下文里"她从没说过这句"——后续回复会像失忆一样
        重复报到（如晨起 DM 问完好、冲刷轮又演一遍刚醒）。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            await cm.add_message_pair(
                cid=cid,
                user_message={"role": "user", "content": "[系统标记：她主动发来下面这条私聊消息]"},
                assistant_message={"role": "assistant", "content": text},
            )
        except Exception:
            logger.warning("[主动私聊] 主动消息归档对话历史失败（不影响发送）", exc_info=True)

    def _consume_makeup(self):
        """补发指令文件：外部/管理往 data/makeup.json 写 {"kind":"night"} 即触发，读后即删"""
        try:
            p = self._data_dir / "makeup.json"
            if p.exists():
                v = json.loads(p.read_text(encoding="utf-8")).get("kind")
                p.unlink(missing_ok=True)
                return str(v) if v else None
        except Exception:
            logger.warning("[主动私聊] 补发指令文件读取失败", exc_info=True)
        return None

    # ---------- 发送 ----------

    _SEG_PAT = r".*?(?:\r?\n+|[。？！~…]+)|.+$"

    def _split_text(self, text: str) -> list:
        segs = [
            t.strip()
            for t in re.findall(self._SEG_PAT, text, re.DOTALL | re.MULTILINE)
            if t.strip()
        ]
        return segs or [text]

    async def _send_human(self, umo: str, text: str) -> None:
        """拟人发送：按句切分逐条发；下一条的间隔=按它的长度算的打字时间
        （每字 0.10~0.18 秒随机，下限 0.8s、上限 8s）——长句等得久，像真人输入，也防风控"""
        segs = self._split_text(text) if self.c_seg_on else [text]
        for i, seg in enumerate(segs):
            if i:
                typing = min(8.0, max(0.8, len(segs[i]) * random.uniform(0.10, 0.18)))
                await asyncio.sleep(typing)
            await self.context.send_message(umo, MessageChain().message(seg))

    async def _fire(self, kind: str, template: str, only_qq: str = None) -> bool:
        """返回是否真的发出去了——调用方据此记账，没发出不销账、窗口内自动重试。
        only_qq：只发给这一个目标（顺延重试用），None=全员循环"""
        tmap = self._target_map()
        if not tmap:
            logger.info(f"[主动私聊] {kind}：无可用目标/平台，跳过（不销账）")
            return False
        now = time.time()
        if not self._quota_ok(now) and kind not in ("morning", "night", "makeup"):
            return False  # 随机类受频控；晨起/睡前是作息事件，不受间隔限制（仍计总量）
        if int(self._st.get("sent_total", 0)) >= self.c_cap:
            return False
        sent_any = False
        for pid, targets in tmap.items():
            for qq in targets:
                if only_qq and qq != only_qq:
                    continue
                umo = f"{pid}:FriendMessage:{qq}"
                use_tpl = template
                if (
                    kind == "random"
                    and self.c_gate_on
                    and not only_qq  # 顺延重试的闸门由消费端已查，避免重复按住
                    and await self._is_chatting(umo, now)
                ):
                    # 正聊着，此刻插话=答非所问——这个念头先按住，
                    # 随机25~90分钟后再想（时刻不可预测，真人也不会掐着表来找你）
                    self._defer_random(pid, qq, now)
                    logger.info(f"[主动私聊] random -> {qq}: 对方正聊着，念头按住随机顺延")
                    continue
                if kind == "night" and await self._already_said_goodnight(umo):
                    # 聊天里已道过晚安：不重复晚安，改发"去睡了"收尾话（换模板不跳过）
                    logger.info(f"[主动私聊] {kind} -> {qq}: 已道过晚安，改发收尾话")
                    use_tpl = self.tpl_night2
                elif kind == "morning":
                    # 防重复：这个用户在她醒后已先开口（晨间冲刷已带出晨间演出），
                    # 再主动问好=第二次报到，跳过这个目标
                    wake_ts = float((self._sleep_state() or {}).get("wake_at") or 0)
                    if wake_ts and await self._user_spoke_after(umo, wake_ts):
                        logger.info(f"[主动私聊] {kind} -> {qq}: 用户已先开口，跳过（冲刷已带出晨间内容）")
                        sent_any = True
                        continue
                text = await self._gen(use_tpl, umo, kind=kind)  # 按目标逐个生成（上下文/工具各不同）
                try:
                    await self._send_human(umo, text)
                    sent_any = True
                    logger.info(f"[主动私聊] {kind} -> {qq}: {text[:40]}")
                    # R7：带工具的轮次把工具轨迹拼进归档——否则后续上下文里
                    # "她不知道自己刚发过那张图/那首歌"，容易穿帮。
                    # 注记从紧：至多3条、每条结果截25字（归档无硬上限，省的是上下文token）
                    archive_text = text
                    if self._tool_trace:
                        archive_text = (
                            text
                            + "\n[本轮工具："
                            + "；".join(self._tool_trace[:3])
                            + "]"
                        )
                    await self._archive_dm(umo, archive_text)
                except Exception:
                    logger.warning(f"[主动私聊] {kind} 发送失败 {umo}", exc_info=True)
        if sent_any:
            self._st["sent_total"] = int(self._st.get("sent_total", 0)) + 1
            self._st["last_dm_ts"] = now
            self._save_state()
        else:
            logger.warning(f"[主动私聊] {kind}：全部目标发送失败")
        return sent_any

    def _record_text(self, content) -> str:
        """history 记录的 content 可能是 str 或分段 list，取纯文本"""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(getattr(p, "text", None), str):
                    parts.append(p.text)
            return "".join(parts).strip()
        return ""

    async def _already_said_goodnight(self, umo: str) -> bool:
        """她最近是否已在聊天里说过晚安（最近12条记录内，assistant侧含'晚安'）
        ——睡前主动消息防重复：道过就不再主动补一句"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return False
            conv = await cm.get_conversation(umo, cid)
            history = getattr(conv, "history", None)
            if isinstance(history, str):
                history = json.loads(history)
            if not isinstance(history, list):
                return False
            for rec in reversed(history[-12:]):
                if isinstance(rec, dict) and rec.get("role") == "assistant":
                    if "晚安" in self._record_text(rec.get("content", "")):
                        return True
                    break  # 只看最后一条她的话：道别后若又聊开了，晚安场景已翻篇
            return False
        except Exception:
            return False

    async def _user_spoke_after(self, umo: str, ts: float) -> bool:
        """该会话用户在她醒来(ts)之后是否已发过消息——用于晨起问候防重复
        （用户先开口=晨间冲刷已发生，她已在对话里报过到）。"""
        spoke = await self._last_user_ts(umo)
        return bool(spoke) and spoke > ts

    async def _last_user_ts(self, umo: str) -> float:
        """对方（user侧）最后一条消息的时间，0=解析不到。
        时间从最后一条 user 记录内嵌的时间戳解析（chat_plus 存档格式
        "[YYYY-MM-DD 周X HH:MM:SS]"），解析不到返回0（调用方放行，不误伤）。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return 0
            conv = await cm.get_conversation(umo, cid)
            history = getattr(conv, "history", None)
            if isinstance(history, str):
                history = json.loads(history)
            if not isinstance(history, list):
                return 0
            for rec in reversed(history):
                if not (isinstance(rec, dict) and rec.get("role") == "user"):
                    continue
                txt = self._record_text(rec.get("content", ""))
                mm = re.search(r"(\d{4})-(\d{2})-(\d{2})[^\d]{1,4}(\d{2}):(\d{2}):(\d{2})", txt)
                if not mm:
                    return 0
                return datetime(
                    int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                    int(mm.group(4)), int(mm.group(5)), int(mm.group(6)),
                ).timestamp()
            return 0
        except Exception:
            return 0

    async def _is_chatting(self, umo: str, now: float) -> bool:
        """活跃度闸门：对方最后一条消息在 window 分钟内=正聊着，此刻别插主动消息"""
        last = await self._last_user_ts(umo)
        return bool(last) and now - last < self.c_gate_window * 60

    def _roll_defer_at(self, now: float) -> float:
        return now + random.uniform(self.c_gate_defer_min, self.c_gate_defer_max) * 60

    def _defer_random(self, pid: str, qq: str, now: float) -> None:
        """把一个"想念念头"按住：随机 defer_min~defer_max 分钟后再想。
        已在队列则只刷新时刻（保留首次按住时间，用于累计上限）"""
        defer = self._st.get("random_defer") or {}
        k = f"{pid}:{qq}"
        old = defer.get(k) or {}
        defer[k] = {
            "pid": pid,
            "qq": qq,
            "at": self._roll_defer_at(now),
            "first": float(old.get("first") or now),
            "tries": int(old.get("tries") or 0),
        }
        self._st["random_defer"] = defer
        self._save_state()

    async def _recent_history(self, umo: str) -> str:
        """你们这个会话最近的聊天记录（末尾 N 条），给主动消息接话茬用"""
        if not self.c_with_ctx:
            return ""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await cm.get_conversation(umo, cid)
            history = getattr(conv, "history", None)
            if not history:
                return ""
            if isinstance(history, str):
                history = json.loads(history)
            if not isinstance(history, list):
                return ""
            lines = []
            for rec in history:
                if not isinstance(rec, dict):
                    continue
                t = self._record_text(rec.get("content", ""))
                if not t:
                    continue
                who = "你" if rec.get("role") == "assistant" else "对方"
                lines.append(f"{who}: {t[:80]}")
            if not lines:
                return ""
            lines = lines[-self.c_ctx_n:]
            return "[你们最近的聊天记录（末尾几条，供接话茬）]\n" + "\n".join(lines) + "\n\n"
        except Exception:
            return ""

    # ---------- 工具调用（v1.3.0）：合成事件 + tool_loop_agent + 白名单过滤 ----------

    _TOOL_HINT = "（如有可用的工具，按你的判断使用）"

    def _guard_tool(self, t):
        """R6：工具 handler 返回 None 时框架会直接终止循环并抛错（tool_loop_agent
        "no final LLM response"→回落纯文本），回落的话术不知道工具刚干了什么——
        副作用已发（语音/卡片）或已报错，她再若无其事说话=双穿帮。
        包一层把 None 换成给模型的说明串，循环能走完、最终话术知道工具的结果；
        顺带记调用轨迹（R7 归档用）。在副本上包，不动共享注册表里的原工具。"""
        import copy as _copy
        import inspect

        orig = getattr(t, "handler", None)
        if orig is None:
            return t
        if inspect.isasyncgenfunction(orig) or inspect.isasyncgenfunction(
            getattr(orig, "func", None)
        ):
            return t  # 生成器型工具（yield 消息组件）语义不同，不包
        tool_name = getattr(t, "name", "?")
        plugin = self

        async def _guarded(*a, **kw):
            rv = await orig(*a, **kw)
            if rv is None:
                plugin._tool_trace.append(f"{tool_name}→无返回值(可能已直接发送)")
                return (
                    "（工具没有文本返回。若它已把语音/图片/卡片直接发给了对方，"
                    "接下来自然衔接一句就好，别重复描述内容；若其实什么都没发出去，"
                    "就如实说没成，别装作成功。）"
                )
            plugin._tool_trace.append(f"{tool_name}→{str(rv)[:25]}")
            return rv

        t2 = _copy.copy(t)
        t2.handler = _guarded
        return t2

    def _tools_for(self, kind: str, scenario: str = ""):
        """按触发类型取工具子集。返回 (ToolSet或None, 是否启用)。
        三路独立：dm_tools_morning / night / random；开关关或工具集空 → None（纯文本生成）
        v1.5 情景门控：scenario 在情景表里时，白名单再与该情景的工具集求交集——
        交集空=该情景纯文本（用户明确配了"这情景别带礼物"）"""
        cfg = {
            "morning": (self.c_tools_morning_on, self.c_tools_morning_names),
            "night": (self.c_tools_night_on, self.c_tools_night_names),
            "makeup": (self.c_tools_night_on, self.c_tools_night_names),  # 补发跟随睡前
            "random": (self.c_tools_random_on, self.c_tools_random_names),
        }.get(kind)
        if not cfg or not cfg[0]:
            return None
        allow = {str(n).strip() for n in (cfg[1] or []) if str(n).strip()}
        if scenario and scenario in self.c_scenario_map:
            scene_tools = self.c_scenario_map[scenario]
            allow = (allow or {str(n) for n in self._all_tool_names()}) & scene_tools
            if not allow:
                logger.info(f"[主动私聊] 情景「{scenario}」许可集为空，本轮纯文本")
                return None
        try:
            mgr = self.context.get_llm_tool_manager()
            full = mgr.get_full_tool_set()
        except Exception:
            logger.warning("[主动私聊] 取工具集失败，本轮纯文本", exc_info=True)
            return None

        def _all_names():
            try:
                return {
                    getattr(t, "name", None) or getattr(t, "handler_name", "")
                    for t in getattr(full, "tools", None) or []
                    if getattr(t, "active", True)
                }
            except Exception:
                return set()

        if not allow:
            allow = _all_names() or None  # 空=全部已激活
        from astrbot.core.agent.tool import ToolSet

        subset = ToolSet()
        try:
            for t in getattr(full, "tools", None) or []:
                if not getattr(t, "active", True):
                    continue  # 未激活/被禁的工具一律不放行（空名单=全部"已激活"工具）
                name = getattr(t, "name", None) or getattr(t, "handler_name", "")
                if allow and name not in allow:
                    continue
                subset.add_tool(self._guard_tool(t))
        except Exception:
            logger.warning("[主动私聊] 工具白名单过滤失败，本轮纯文本", exc_info=True)
            return None
        if subset.empty():
            logger.info(f"[主动私聊] {kind} 白名单未命中任何已注册工具，本轮纯文本")
            return None
        return subset

    def _all_tool_names(self) -> list:
        """情景门控用：当前全部已激活工具名（取不到返回空表）"""
        try:
            full = self.context.get_llm_tool_manager().get_full_tool_set()
            return [
                getattr(t, "name", None) or getattr(t, "handler_name", "")
                for t in getattr(full, "tools", None) or []
                if getattr(t, "active", True)
            ]
        except Exception:
            return []

    def _synthetic_event(self, umo: str):
        """构造合成事件（参照框架 CronMessageEvent 的做法）：
        工具 handler 需要 event（读会话/发卡片），dm 是定时器触发没有真事件——
        造一个指向目标私聊会话的假事件，副作用型工具的 event.send 会路由到真实会话"""
        try:
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession

            session = MessageSession.from_str(umo)
            return CronMessageEvent(
                context=self.context,
                session=session,
                message="[主动消息任务]",
                sender_name=PLUGIN_NAME,
            )
        except Exception:
            logger.warning("[主动私聊] 合成事件构造失败，工具不可用本轮", exc_info=True)
            return None

    _INNER_TASK = (
        "现在只在心里想，不发消息、不用组织成要发的话：你此刻的感觉、"
        "想起{who}的什么、想说点什么、想给{who}看点什么（想画/想分享什么都行）。"
        "几句话，随意想。"
    )

    async def _inner_monologue(self, persona_text: str, ctx: str, who: str, veto_hint: str = "") -> str:
        """两段式第一段：无工具酝酿内心戏。产物只拼进第二段 prompt，
        不发送、不归档、不落盘（防记忆污染）。失败抛异常由调用方降级单段。
        v1.5：veto_hint 非空时内心戏顺带做"发/不发"判断（AI 否决层，只有否决权）。"""
        try:
            provider = await self.context.get_using_provider_async(None)
        except Exception:
            provider = self.context.get_using_provider()
        prompt = (
            (persona_text[:1500] + "\n\n" if persona_text else "")
            + ctx
            + f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n"
            + self._INNER_TASK.replace("{who}", who)
        )
        if veto_hint:
            prompt += "\n" + veto_hint.replace("{who}", who)
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, session_id=f"{PLUGIN_NAME}_inner"),
            timeout=30,
        )
        self._ledger_add("inner")
        return str(getattr(resp, "completion_text", "") or "").strip()[:300]

    async def _gen_with_tools(self, prompt: str, umo: str, tools) -> str:
        """带工具循环的生成：模型可自主调用白名单工具（执行由框架 agent 循环完成），
        最终返回完整文本。失败抛异常由调用方回落。"""
        self._tool_trace = []  # 双保险（_gen 已清；直接调用本方法的测试/路径也清）
        ev = self._synthetic_event(umo)
        if ev is None:
            raise RuntimeError("合成事件构造失败")
        prov_id = None
        try:
            prov_id = await self.context.get_current_chat_provider_id(umo)
        except Exception:
            pass
        if not prov_id:
            try:
                p = await self.context.get_using_provider_async(None)
                prov_id = p.meta().id
            except Exception:
                raise RuntimeError("无法确定聊天 provider")
        resp = await asyncio.wait_for(
            self.context.tool_loop_agent(
                event=ev,
                chat_provider_id=prov_id,
                prompt=prompt + "\n" + self._TOOL_HINT,
                tools=tools,
                max_steps=4,
                tool_call_timeout=90,
            ),
            timeout=150,
        )
        return str(getattr(resp, "completion_text", "") or "").strip()

    async def _gen(self, template: str, umo: str = "", kind: str = "",
                   veto_ok: bool = False, veto_ctx: str = "", scenario: str = "") -> str:
        """首选 LLM 临场生成（现用人设 + 最近聊天上下文 + 当前时间）；
        kind 对应触发配置了可用工具时走带工具的 agent 循环；失败/超时回落语料池。
        v1.5：①template 模式直接抽语料池（零 token）；②veto_ok 时内心戏顺带否决判断，
        返回 None=被否决（self._last_gen_status 记原因）；③段间终检（生成中途对方来消息
        →只废内心戏一次，正文不跑）；④scenario 情景门控工具。"""
        self._tool_trace = []  # 每次生成都清空，防上一个目标的轨迹串到下一个（纯文本路径也要清）
        self._last_gen_status = "ok"
        self._last_veto_reason = ""
        if kind and self.c_gen_mode.get(kind) == "template":
            self._last_gen_status = "template"
            return random.choice(self.c_pool)
        who = self.c_alias
        try:
            qq = umo.split(":")[2] if umo else ""
            who = self.c_alias_map.get(qq, self.c_alias)
        except Exception:
            pass
        prompt_tpl = template.replace("{who}", who)
        act_ts_before = float((self._st.get("sess", {}).get(umo) or {}).get("act_ts") or 0)
        try:
            try:
                provider = await self.context.get_using_provider_async(None)
            except Exception:
                provider = self.context.get_using_provider()
            if provider is not None:
                persona_text = ""
                try:
                    persona = await self.context.persona_manager.get_default_persona_v3(None)
                    if isinstance(persona, dict):
                        persona_text = str(persona.get("prompt") or persona.get("system_prompt") or "")
                    elif persona is not None:
                        persona_text = str(
                            getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "")
                        )
                except Exception:
                    persona_text = ""
                ctx = await self._recent_history(umo) if umo else ""
                if veto_ctx:
                    ctx = veto_ctx + ctx  # 状态事实块放在聊天记录前
                now_line = f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n"
                prompt = (
                    (persona_text[:1500] + "\n\n" if persona_text else "")
                    + ctx
                    + now_line
                    + prompt_tpl
                )
                # v1.5 否决层：内心戏阶段顺带判断发/不发（搭同一次调用，不额外烧）
                inner = ""
                if veto_ok and self.c_two_stage:
                    try:
                        inner = await self._inner_monologue(
                            persona_text, ctx, who,
                            veto_hint=self.tpl_veto_hint.replace("{who}", who),
                        )
                    except Exception:
                        logger.info("[主动私聊] 内心戏生成失败，本轮单段直出（否决随fail-open放行）")
                    if inner and self._veto_marked(inner):
                        self._last_gen_status = "veto"
                        return None  # 一票否决：只有拦的权，没有催的权
                    # 段间终检：内心戏期间对方来消息 → 只废这一段
                    cur = float((self._st.get("sess", {}).get(umo) or {}).get("act_ts") or 0)
                    if umo and act_ts_before and cur != act_ts_before:
                        self._last_gen_status = "void_inner"
                        self._ledger_add("void_gen")
                        return None
                # 工具分支：该触发类型配置了工具 → 带工具循环生成（耗时更长，超时放宽）
                tools = self._tools_for(kind, scenario) if kind else None
                if tools is not None:
                    try:
                        p2 = prompt
                        if self.c_two_stage:
                            # 两段式第一段：先无工具酝酿内心戏（不发送不归档），
                            # 第二段带着它进工具循环——治"任务指令+工具回执"
                            # 双指令源挤一次冷启动生成导致的割裂
                            if not inner:
                                try:
                                    inner = await self._inner_monologue(persona_text, ctx, who)
                                except Exception:
                                    logger.info("[主动私聊] 内心戏生成失败，本轮单段直出")
                            if inner:
                                now2 = f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n"
                                p2 = (
                                    (persona_text[:1500] + "\n\n" if persona_text else "")
                                    + ctx
                                    + now2
                                    + "[你刚才心里想的话（这段不会发出去，"
                                      "仅供你组织这次的表达，别复述它）]\n"
                                    + inner
                                    + "\n\n"
                                    + prompt_tpl
                                )
                        text = await self._gen_with_tools(p2, umo, tools)
                        self._ledger_add("gen")
                        self._ledger_add("tools")
                        if text:
                            return text[:200]
                        logger.info("[主动私聊] 工具循环无文本产出，回落纯文本生成")
                    except Exception:
                        logger.warning("[主动私聊] 工具生成失败，回落纯文本", exc_info=True)
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, session_id=f"{PLUGIN_NAME}_gen"),
                    timeout=25,
                )
                self._ledger_add("gen")
                text = str(getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text[:120]
        except Exception:
            logger.warning("[主动私聊] LLM 生成失败，回落语料池", exc_info=True)
        return random.choice(self.c_pool)

    def _veto_marked(self, inner: str) -> bool:
        """内心戏首段是否给了否决标记【不发】/[不发]；记下理由"""
        first = inner.strip().splitlines()[0] if inner.strip() else ""
        if ("【不发】" in first) or ("[不发]" in first):
            self._last_veto_reason = first.replace("【不发】", "").replace("[不发]", "").strip() or inner.strip()[:60]
            return True
        return False

    # ---------- 调试指令（仅管理员） ----------

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    @filter.command("主动私聊状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        self._rollover_day()
        sl = self._sleep_state() or {}
        m = self._st.get("morning") or {}
        n = self._st.get("night") or {}
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%H:%M") if t else "-"
        lines = [
            f"[主动私聊 v{PLUGIN_VERSION}] 今日 {self._st.get('date')}"
            f" 模式：{'静默触发' if self.c_miss_mode == 'silence' else '旧定时器'}",
            f"目标：{'/'.join(self._targets()) or '（空）'} 平台：{self._platform_id() or '?'}",
            f"作息联动：{'读到了' if sl else '无睡觉数据（当白天跑）'} 当前相：{self._sleep_phase()}",
            f"晨起：{'已发' if m.get('sent') else (fmt(m.get('at')) if m else '无计划')}"
            f" 睡前：{'已发' if n.get('sent') else (n.get('key') or '无计划')}",
        ]
        if self.c_miss_mode == "silence":
            lo, hi = self._quiet_window()
            lines.append(
                f"免打扰：{lo//60:02d}:{lo%60:02d}~{hi//60:02d}:{hi%60:02d}"
                f"（{self.c_quiet_mode}） 未回应熔断阈值：{self.c_breaker}"
            )
            lines.append(
                f"想念：今日 {self._st.get('random_sent', 0)}/{self.c_daily_max}"
                f" 总量 {self._st.get('sent_total', 0)}/{ABS_TOTAL_CAP}（当日硬顶），"
                f"上次发送 {fmt(self._st.get('last_dm_ts')) if self._st.get('last_dm_ts') else '今天还没发过'}"
            )
            # 每目标一行：骰子状态 + 门
            tmap = self._target_map()
            for pid, qqs in tmap.items():
                for qq in qqs:
                    umo = f"{pid}:FriendMessage:{qq}"
                    s = self._sess(umo)
                    d = s.get("dice") or {}
                    if d.get("rolled"):
                        dice_line = (
                            f"骰已掷({'中' if d.get('win') else '未中'} p={d.get('p', 0):.2f}"
                            f" 事件{fmt(d.get('event'))})"
                        )
                    else:
                        dice_line = "骰未掷（等静默达标）"
                    pd = s.get("pending") or {}
                    pend = f"→定发{fmt(pd.get('at'))}" if pd.get("at") else ""
                    mu = s.get("mute_until") or 0
                    mute = f" 抑制至{datetime.fromtimestamp(mu).strftime('%m-%d %H:%M')}" if mu > datetime.now().timestamp() else ""
                    brk = f" 未回应{s.get('unans', 0)}" if s.get("unans") else ""
                    tier = f" 挡位[{self._tier_of(umo)}]" if self.c_tier_per_conf else ""
                    lines.append(f"· {qq}：{dice_line}{pend}{mute}{brk}{tier}")
        else:
            lines.append(
                f"随机想念：今日 {self._st.get('random_sent', 0)}/{self.c_random_n} 发，"
                f"剩余计划 {[fmt(t) for t in (self._st.get('random_plan') or [])]}"
            )
            lines.append(
                f"总量 {self._st.get('sent_total', 0)}/{self.c_cap}，"
                f"上次发送 {fmt(self._st.get('last_dm_ts')) if self._st.get('last_dm_ts') else '今天还没发过'}"
            )
        led = self._st.get("ledger") or {}
        lines.append(
            f"台账：内心{led.get('inner', 0)} 生成{led.get('gen', 0)}"
            f" 工具{led.get('tools', 0)} 被否决{led.get('vetoed', 0)} 废稿{led.get('void_gen', 0)}"
        )
        yield event.plain_result("\n".join(lines))

    def _miss_day_stats(self, qq: str) -> dict:
        """某人今天的想念台账：从 dlog 按当日过滤（ts 是 MM-DD HH:MM）。
        想起=掷骰次数（含没中和中签）；忍住=没中+终检拦下+她自己拦下"""
        today = datetime.now().strftime("%m-%d")
        st = {"rolls": 0, "win": 0, "lose": 0, "void": 0, "veto": 0, "sent": 0}
        for e in (self._st.get("dlog") or []):
            if str(e.get("umo") or "") != str(qq) or not str(e.get("ts") or "").startswith(today):
                continue
            t = e.get("t")
            if t == "miss_lose":
                st["rolls"] += 1
                st["lose"] += 1
            elif t == "miss_win":
                st["rolls"] += 1
                st["win"] += 1
            elif t == "miss_void":
                st["void"] += 1
            elif t == "miss_veto":
                st["veto"] += 1
            elif t == "miss_sent":
                st["sent"] += 1
        return st

    @filter.command("主动私聊想你")
    async def cmd_miss_count(self, event: AstrMessageEvent):
        """今天的想念台账：想起几次、来了几次、忍住几次（忍住的原因也拆开）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        lines = [f"[主动私聊] {datetime.now().strftime('%m-%d')} 想你台账"]
        for pid, qqs in (self._target_map() or {}).items():
            for qq in qqs:
                st = self._miss_day_stats(qq)
                if not any(st.values()):
                    lines.append(f"· {qq}：今天还没想起过你")
                    continue
                held = st["lose"] + st["void"] + st["veto"]
                why = []
                if st["lose"]:
                    why.append(f"没中{st['lose']}")
                if st["void"]:
                    why.append(f"终检拦下{st['void']}")
                if st["veto"]:
                    why.append(f"她自己拦下{st['veto']}")
                lines.append(f"· {qq}：想起 {st['rolls']} 次 → 来了 {st['sent']} 次，"
                             f"忍住 {held} 次（{'·'.join(why)}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("主动私聊工具")
    async def cmd_tools(self, event: AstrMessageEvent, args=None):
        """列出当前所有可用的 llm_tool（按插件分组·名字·说明），
        供配置三路白名单时照抄。默认只列启用中的工具（禁用的刷屏且配了也不生效），
        带"全部"参数才展开禁用项"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        show_all = any(k in str(args or "") for k in ("全部", "all", "含禁用"))
        try:
            full = self.context.get_llm_tool_manager().get_full_tool_set()
            tools = list(getattr(full, "tools", None) or [])
        except Exception:
            yield event.plain_result("工具管理器不可用，看日志")
            return
        # 按插件分组；禁用项聚合为每组一行尾注（默认隐藏——白名单过滤本就不放行它们）
        # 两套行文：full（转发节点用，完整介绍——折叠的不占地方）/ short（回落纯文本用，24字截断）
        groups: dict = {}
        groups_short: dict = {}
        disabled_count: dict = {}
        for t in tools:
            name = getattr(t, "name", "?")
            active = getattr(t, "active", True)
            plugin = "其他"
            mod = getattr(t, "handler_module_path", None)
            if mod:
                try:
                    from astrbot.core.star.star_handler import star_map

                    p = star_map.get(mod)
                    plugin = getattr(p, "display_name", None) or getattr(p, "name", "") or "其他"
                except Exception:
                    pass
            if not active:
                disabled_count[plugin] = disabled_count.get(plugin, 0) + 1
                if not show_all:
                    continue
            raw = (getattr(t, "description", "") or "").strip()
            full = "；".join(x for x in raw.split("\n") if x.strip())[:400] or "（无描述）"
            short = raw.split("\n")[0][:24] + ("…" if len(raw.split("\n")[0]) > 24 else "")
            if not short:
                short = "（无描述）"
            state = "" if active else "（禁）"
            groups.setdefault(plugin, []).append(f"· {name} — {full}{state}")
            groups_short.setdefault(plugin, []).append(f"· {name} — {short}{state}")
        # 禁用尾注
        for g, n in disabled_count.items():
            if not show_all:
                note = f"（另有 {n} 个已禁用，发「主动私聊工具 全部」展开）"
                groups.setdefault(g, []).append(note)
                groups_short.setdefault(g, []).append(note)
        if not groups:
            groups = {"（当前没有任何已注册的工具）": []}
            groups_short = groups
        # 每个插件一个小节，节与节之间空一行；单条消息超长自动分段（QQ 长文本上限约 4500，
        # 保守 1600 分段，分段只在节/行边界切，不撕断行）
        blocks = [
            (f"【{g}】\n" + "\n".join(ts)) if ts else f"【{g}】" for g, ts in groups.items()
        ]
        blocks_short = [
            (f"【{g}】\n" + "\n".join(ts)) if ts else f"【{g}】" for g, ts in groups_short.items()
        ]

        def _line(label, on, names):
            lst = f"：{'、'.join(names)}" if names else "：空 = 全部工具"
            return f"· {label}（{'开' if on else '关'}）{lst}"

        tail = (
            "📋 当前白名单\n"
            + _line("晨起", self.c_tools_morning_on, self.c_tools_morning_names)
            + "\n"
            + _line("睡前", self.c_tools_night_on, self.c_tools_night_names)
            + "\n"
            + _line("随机", self.c_tools_random_on, self.c_tools_random_names)
            + "\n\n"
            "配置：面板 → 主动私聊 → 触发 → 对应白名单（留空=全部）"
        )

        # 优先发"聊天记录"（合并转发）：不占聊天空间；每个插件一节、白名单收尾。
        # 转发组件不可用/发送失败时回落分段纯文本
        try:
            from astrbot.core.message.components import Node, Nodes, Plain as _Plain

            try:
                self_id = str(event.get_self_id())
            except Exception:
                self_id = "0"
            try:
                self_name = str(event.get_self_name() or "bot")
            except Exception:
                self_name = "bot"
            node_list = [
                Node(
                    content=[_Plain("🧰 主动私聊 · 可用工具清单（每节一个插件）")],
                    name=self_name,
                    uin=self_id,
                )
            ]
            node_list += [
                Node(content=[_Plain(b)], name=self_name, uin=self_id) for b in blocks
            ]
            node_list.append(Node(content=[_Plain(tail)], name=self_name, uin=self_id))
            yield event.chain_result([Nodes(node_list)])
            return
        except Exception:
            logger.warning("[主动私聊] 转发消息发送失败，回落纯文本", exc_info=True)

        # QQ 客户端会折叠空行——分隔用真实字符的分隔线，怎么折叠都看得见
        SEP = "\n————————————\n"
        MAX_LEN = 1600
        chunks, cur = [], "🧰 主动私聊 · 可用工具\n"
        for b in blocks_short:
            add = (SEP if len(cur) > len("🧰 主动私聊 · 可用工具\n") else "") + b
            if len(cur) + len(add) > MAX_LEN:
                chunks.append(cur)
                cur = "🧰 主动私聊 · 可用工具（续）\n" + b
            else:
                cur += add
        if len(cur) + len(SEP) + len(tail) > MAX_LEN:
            chunks.append(cur)
            chunks.append(tail)
        else:
            chunks.append(cur + SEP + tail)
        for c in chunks:
            yield event.plain_result(c)

    @filter.command("主动私聊测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """立刻给发送者本人私聊来一条（验证通道+人设）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        umo = str(event.unified_msg_origin)
        pid = umo.split(":")[0]  # 发送者所在平台
        sid = str(event.get_sender_id())
        text = await self._gen(self.tpl_random, umo=f"{pid}:FriendMessage:{sid}", kind="random")
        try:
            await self._send_human(f"{pid}:FriendMessage:{sid}", text)
            yield event.plain_result(f"已私聊发送：{text[:60]}（测试不计入当日频控）")
        except Exception:
            yield event.plain_result("发送失败，看日志")
