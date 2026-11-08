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
try:
    from astrbot.api.message_components import At as _AtComp
except Exception:
    _AtComp = None
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.session_llm_manager import SessionLLMManager
except ImportError:
    SessionLLMManager = None

PLUGIN_NAME = "astrbot_plugin_dafeiyu_dm"
PLUGIN_VERSION = "2.7.7"
DM_PERSONA_SLICE = 999999  # v2.2.0 切片2600→v2.5.12 放开全量（用户拍板）：主动消息看完整人设，底线下沉段/语气参考不再窗外；主动消息日均≤7条，全量约+1.3K token/条，忽略不计
SLEEP_STATE_CANDIDATES = [
    Path("data/plugin_data/astrbot_plugin_dafeiyu_sleep/sleep_state.json"),
]
TICK_SECONDS = 30
NIGHT_WINDOW_MIN = 10  # 入睡点前多少分钟内发睡前消息
ABS_TOTAL_CAP = 7     # 跨类型每日总量绝对上限（安全件，不可配）
# v2.5.0 D7 发送熔断：连续失败 N 次 → 停发该会话，指数退避 30min 起翻倍至 12h 封顶
SEND_FAIL_N = 3
SEND_BREAK_BASE_MIN = 30
SEND_BREAK_CAP_MIN = 12 * 60
SEEN_WHISPER_WIN = 600  # B5：掷中前 10 分钟内他在群里说过话 → 悄悄话口吻
# v2.6.0 日切：醒来开新对话+昨日摘要种子（F②落地——治"每条消息驮全部历史"的账单锯齿）
DAY_CUT_WAKE_WINDOW = 6 * 3600   # 醒后多久内可切（错过窗口=当天不切）
DAY_CUT_MIN_HIST = 20            # 昨天少于这个条数不值得摘要，直接销账不切
DAY_CUT_RETRY_MIN = 10           # 种子生成失败重试间隔（分钟）
DAY_CUT_MAX_TRIES = 12           # 最多重试 12 次≈2 小时，全失败回落旧行为（记忆无损）
QUIET_HARD_LO = 8 * 60    # 评估时段硬边界 08:00
QUIET_HARD_HI = 23 * 60 + 30  # …~23:30（风控：深夜不评估不发送）
AFK_GRACE_MIN = 5.0  # 离席/约定词宽限：词说出后对话在5分钟内结束→仍按该档（"吃饭啦""拜拜"分开发不丢档）
# v2.5.7 离席词自称白名单（用户思路：确认"我本人"才锚，人名黑名单防不完）：
# 词前6字窗口剔除自称/铺垫词+标点语气词后必须"什么都不剩"——剩了就是别人的话（朋友/小王/他），不锚
_AFK_SELF_FILLERS = ("准备去", "马上要", "一起", "准备", "马上", "这就", "现在", "真的",
                     "好啦", "我", "咱", "又", "再", "先", "要", "得", "去", "该", "也", "忙", "好")
# v2.3.0 回归问候：离席词预期时长先验（分钟；被学习表中位数覆盖）
AFK_DUR_PRIOR = {"洗澡": 45, "洗漱": 45, "吃饭": 75, "干饭": 75, "出门": 90, "取快递": 90,
                 "下班": 60, "打游戏": 150, "上课": 480, "上班": 540, "加班": 600}
MOOD_STEP = 0.15        # 心情节律随机游走幅度 ±15%
# v2.3.0 回归问候曲线常量（不暴露配置）
AFK_CHECK_WIN_MIN = 90            # 回归窗宽（分钟）
AFK_CHECK_PEAK_MIN = 20           # 对数正态 hazard 峰位（开窗后分钟）
AFK_CHECK_SIGMA = 0.6             # 对数正态 σ
INTENT_POOL_PROMISE = (1.2, 1.5)  # 约定在身掷骰池（明确契约，冷热少打折）
# v2.7.0 约的锚点三分类 + 重掷间隔乘子（2026-11-05 用户拍板"做全套"，设计稿=dm_v270_有约锚点_设计文档.md）
VAGUE_DUR_PRIOR = {"等会": 45, "等下": 45, "一会": 45, "待会": 45, "晚点": 90, "稍后": 90,
                   "回来": 90, "回头": 150, "过后": 150, "弄完": 150}  # 模糊词梯度先验（分钟）
PROMISE_TIME_WORDS = ("晚上", "今晚", "夜里", "傍晚")   # 时刻词："晚上找你"→今晚钟点锚
PROMISE_TIME_WIN = (19 * 60, 20 * 60)                  # 时刻词锚窗：19:00~20:00 随机一分钟
PROMISE_EXPIRE_H = 2.0            # 锚过这小时数还没等到人：销约（缺席留痕）
RETRY_MULT_HEAT = (0.45, 0.7, 1.0)   # 重掷间隔乘子：热/中/冷
RETRY_MULT_PROMISE = 0.4             # 有约在身：念想重得快
RETRY_MULT_FREE = 0.6                # 空闲日：闲着更容易想起来
RETRY_FLOOR_MIN = 45                 # 重掷间隔下限（分钟）
ANCHOR_MOOD_HI, ANCHOR_MOOD_LO = 1.05, 0.90   # 心情挪锚阈值（只动时机不动概率）
ANCHOR_MOOD_FAST, ANCHOR_MOOD_SLOW = 0.7, 1.5  # 高涨→锚提前30% / 蔫→推后50%
# v2.7.3 赴约时机细化（设计稿第七轮定稿，常量不进面板——零新键纪律）
FAKE_PROMISE_MIN = 8.0      # 假约线：说词后初始链拖超这分钟数=约作废（哪有说完又聊七八分钟的）
EARLY_RATIO = 0.6           # 3/5 线：等了不到预期60%就赴约=过早，样本不入池
EARLY_STREAK_N = 4          # 连续第4次过早才开始入池（节奏真变了），正常时长即清零
DEFER_LO, DEFER_HI = 40, 80  # 中签撞回归：票顺延区间（分钟），只顺延一次
ANCHOR_CAP_MARGIN = 15      # 夜约钳窗尾：锚不越今晚评估窗尾-15min（穿窗否决）
GENERIC_RETURN_WORDS = ("回来啦", "我回来了", "回来了呀", "来找你了", "来找你啦",
                        "找你啦", "找你来了")  # 通用回归词：不管约的是什么都算赴约
# 普通/告别池随 T 弹性：c_plain=0.55+0.3T / c_bye=min(0.35+0.5T,0.7)，池=c±0.1，告别顶钳 0.8
REUNION_E_LO, REUNION_E_HI = 6 * 3600, 36 * 3600   # 重逢 E_eff 钳位（秒）
REUNION_TIERS = ((2, 48 * 3600, 72 * 3600),        # (倍数, 绝对下限, 绝对上限) 轻哼 2~3 天
                 (4, 96 * 3600, 120 * 3600),        # 明显 4~5 天
                 (8, 168 * 3600, 240 * 3600))       # 大委屈 7~10 天封顶
SULK_THR_BASE_H, SULK_THR_SPAN_H = 3.0, 2.0        # 小委屈阈值中心=3h−2h×热度
SULK_THR_CLAMP = (40 / 60, 4.0)                    # 钳 40min~4h
SULK_NIGHT_ADD_H = 8.0                             # 夜间 22~09 发出的 +8h（睡着不算晾）
SULK_VALID_H = 48.0                                # 小委屈状态有效期
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
    "dm_memory_on": True,       # v2.2.0：生成前预取长期记忆（livingmemory 按目标会话隔离检索）；没装=自动无效果
    "dm_memory_k": 3,           # 预取几条
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
    "dm_morning_reply_template": (
        "你刚睡醒没多久，翻手机才发现{who}在你睡着的时候给你发了留言。"
        "你主动回TA一条——\"刚醒，看到你发的消息了\"的意思，自然地回应留言里的内容"
        "（TA说了什么就接什么，该答的答、该撒娇的撒娇，按你们的相处来）。"
        "一两句，刚醒的状态自然带出来。别提任何系统或作息数字。"
    ),
    "dm_morning_reply_map": [],     # v2.5.2 "配置ID前缀=模板" / "QQ=模板" / "default=模板"——留言回应模板按人分流（私货版给指定号，其余落全局默认）
    "dm_day_cut_on": False,        # v2.6.0 日切：醒来开新对话+昨日摘要种子（公共默认关，实例按需开）
    "dm_group_at_counts": True,    # v2.6.3 群里@她算不算"理她"：开=群聊陪她想念钟重置；关=群聊不算私聊陪伴，她照常来私聊找你
    "dm_day_cut_seed_template": (
        "你在把一段用户与AI角色的私聊记录浓缩成次日记忆摘要，这段摘要会成为她新一天醒来时的记忆。"
        "用中文按时间顺序写成要点，全文控制在1000字内。必须保留：①用户的个人信息与喜好"
        "（称呼、习惯、最近在忙的事、喜欢和讨厌的东西）；②双方的约定、承诺、待办和已定的提醒"
        "（含具体日期时间）；③聊到一半还没收尾的话题（今天要能接得上）；④两人关系的进展和情绪事件"
        "（和好、闹别扭、说开的心事、气氛变化）；⑤只有两人懂的梗、玩笑和专属叫法。"
        "空间不够按上面顺序取舍。纯寒暄、已完成的琐事略去。如果记录里已有旧摘要，"
        "把新内容合并进旧摘要更新它。不要写成流水账，写成「发生了什么、定了什么、还差什么」。"
    ),     # v2.5.2 "配置ID前缀=模板" / "QQ=模板" / "default=模板"——留言回应模板按人分流（私货版给指定号，其余落全局默认）
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
    # v2.7.5 释放词：与离席词同条出现（"吃饭去咯不用等我啦"）=他明说不用等——
    # 短串子串匹配，"不用等我啦/哈/哦"等尾缀不用单独列
    "miss_afk_release_words": [
        "不用等", "别等我", "别等了", "不等我", "不用候着", "不用掐点",
        "你先忙", "你忙你的", "你先玩", "你玩你的", "你先休息",
        "别管我", "不用管我", "别惦记", "不用惦记", "不用回我",
        "要很久", "会很久", "得很久", "很晚才", "得一阵子", "一时半会",
    ],
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
    # —— v2.3.0 ——
    "miss_cd_a_learn": True,          # date 深度耦合 V（他怎么回应她→她的矜持深浅）
    "miss_cd_a_lo": 0.45,
    "miss_cd_a_hi": 0.80,
    "miss_cd_a_slope": 1.0,
    "miss_afk_check_on": False,       # 回归问候（opt-in：新增行为，用户决定）
    "miss_afk_dur_map": [],           # "词=分钟" 覆盖先验
    "miss_afk_refuse_words": ["别找我", "让我静静", "不想聊", "别烦我", "想静静"],
    "miss_afk_bye_words": ["拜拜", "再见", "回见"],
    "dm_afk_check_template": (
        "他之前说{afk_word}离开，按你们的相处这个点该回来了，人却没冒头。"
        "你想主动问一句——「下班了吗」「该理我了吧」的意思，用你自己的说法。"
        "就一两句，别提系统。"
    ),
    "dm_reunion_on": True,            # 重逢委屈（生成侧事实注入）
    "dm_sulk_on": True,               # 被晾小委屈（生成侧事实注入）
    "dm_preset_apply": "",            # v2.4.0 用途预设：companion/balanced/tool，应用后自动清空
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
# ---------- v2.4.0 用途预设包（两下拉一步到位：用途在此选，性格用 miss_temperament） ----------

ORIENT_PRESETS = {
    "companion": {  # 情感陪伴型：全情感栈开
        "dm_miss_mode": "silence", "dm_morning_on": True, "dm_night_on": True,
        "miss_afk_check_on": True, "dm_reunion_on": True, "dm_sulk_on": True,
        "dm_two_stage": True,
        "dm_with_context": True, "dm_memory_on": True, "dm_seg_enabled": True,
        "dm_respect_sleep": True,
    },
    "balanced": {   # 均衡型：现状缺省（想念走概率、委屈开、追问 opt-in）
        "dm_miss_mode": "silence", "dm_morning_on": True, "dm_night_on": True,
        "miss_afk_check_on": False, "dm_reunion_on": True, "dm_sulk_on": True,
        "dm_two_stage": True,
        "dm_with_context": True, "dm_memory_on": True, "dm_seg_enabled": True,
        "dm_respect_sleep": True,
    },
    "tool": {       # 工具助理型：timer 定时 + 全情感栈关（助理不委屈）
        "dm_miss_mode": "timer", "dm_morning_on": False, "dm_night_on": False,
        "miss_afk_check_on": False, "dm_reunion_on": False, "dm_sulk_on": False,
        "dm_two_stage": False,
        "dm_with_context": True, "dm_memory_on": True, "dm_seg_enabled": True,
        "dm_respect_sleep": True,
    },
}

# 提示词族=拼装：3 用途骨架 + 4 性格语气层（组合出 12 种独立成文；中性公共文案）
STANCE_SKELETON = {
    "companion": "你们是彼此亲近的人。主动开口不需要理由，惦记本身就是常态；语气带着你们相处的生活感。",
    "balanced": "你们关系不错。主动开口自然就好，该说事说事，该关心关心，不用端着。",
    "tool": "你是TA的助理。主动消息以把事说清楚为先：简洁、直接、信息完整，不需要寒暄和情绪铺垫。",
}
STANCE_TONE = {
    "平淡": "你的性子淡淡的：话少，不主动煽情，关心藏在简短里。",
    "正常": "你的性子平平常常：该热络热络，该收就收，自然。",
    "热情": "你的性子热络：话带着劲，关心挂在嘴上，藏不住。",
    "病娇": "你的性子又黏又独：想起TA就坐不住，话里带着点占有欲和小情绪。",
}
TOOL_RANDOM_TPL = (
    "到点了，把该说的事说清楚：一句话交代事项本身，需要TA做什么直接写，"
    "不要寒暄铺垫，不要情绪描写。就一两句。"
)

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
        self._cfg_obj = config
        # v2.4.0 用途预设：选了未应用的包 → 就地改写 config 原文（后续读取自然生效），
        # 选择器清空 + 存盘（stance_gen 同款一次性模式）。性格语气层拼进全局心态段
        _raw = config or {}
        _flat0 = self._flatten(_raw)
        _preset = str(_flat0.get("dm_preset_apply") or "").strip()
        if _preset in ORIENT_PRESETS:
            _apply = dict(ORIENT_PRESETS[_preset])
            if _preset == "tool":
                _apply["dm_miss_random_template"] = TOOL_RANDOM_TPL
            _temper = str(_flat0.get("miss_temperament") or "自定义")
            _tone = STANCE_TONE.get(_temper, "")
            if _preset in STANCE_SKELETON:
                _apply["dm_miss_stance_hint"] = STANCE_SKELETON[_preset] + _tone
            _apply["dm_preset_apply"] = ""
            for k, v in _apply.items():
                self._cfg_write_raw(_raw, k, v)
            self._preset_dirty = True  # initialize 里存盘
            cfg = self._flatten(_raw)  # 含预设改写后的读取
        else:
            cfg = self._flatten(_raw)
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
        self.c_memory_on = bool(g("dm_memory_on"))  # v2.2.0 预取长期记忆
        try:
            self.c_memory_k = max(1, min(8, int(g("dm_memory_k") or 3)))
        except Exception:
            self.c_memory_k = 3
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
        self.tpl_morning_reply = str(g("dm_morning_reply_template") or DEFAULTS["dm_morning_reply_template"])
        self.c_day_cut = bool(g("dm_day_cut_on"))
        self.c_group_at_counts = bool(g("dm_group_at_counts"))
        self.tpl_seed = str(g("dm_day_cut_seed_template") or DEFAULTS["dm_day_cut_seed_template"])
        # v2.5.2 留言回应模板按人分流（同心态段路数：QQ > 配置前缀/default > 全局默认）
        self._mr_map = {}
        for _item in (g("dm_morning_reply_map") or []):
            _s2 = str(_item).strip()
            if "=" not in _s2:
                logger.warning(f"[主动私聊] 留言回应模板映射「{_s2}」没有=，跳过")
                continue
            _k2, _, _v2 = _s2.partition("=")
            if not _v2.strip():
                continue
            self._mr_map[("qq:" + _k2) if _k2.strip().isdigit() else ("conf:" + _k2.strip().lower())] = _v2.strip()
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
        self.c_release_words = [str(w).strip() for w in (g("miss_afk_release_words") or []) if str(w).strip()]
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
        # v2.3.0
        self.c_cd_a_learn = bool(g("miss_cd_a_learn"))
        self.c_cd_a_lo = float(g("miss_cd_a_lo") or 0.45)
        self.c_cd_a_hi = float(g("miss_cd_a_hi") or 0.80)
        self.c_cd_a_slope = float(g("miss_cd_a_slope") or 1.0)
        self.c_afk_check_on = bool(g("miss_afk_check_on"))
        self.c_afk_dur_map = {}
        for _s in list(g("miss_afk_dur_map") or []):
            _k, _sep, _v = str(_s).partition("=")
            if _sep and _k.strip() and _v.strip().isdigit():
                self.c_afk_dur_map[_k.strip()] = int(_v)
        self.c_refuse_words = [str(x).strip() for x in (g("miss_afk_refuse_words") or []) if str(x).strip()]
        self.c_bye_words = [str(x).strip() for x in (g("miss_afk_bye_words") or []) if str(x).strip()]
        self.tpl_afk_check = str(g("dm_afk_check_template") or DEFAULTS["dm_afk_check_template"])
        self.c_reunion_on = bool(g("dm_reunion_on"))
        self.c_sulk_on = bool(g("dm_sulk_on"))
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
        self._conf_umo_cache = {}
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
        """umo → 配置文件ID。v2.2.2 优先官方 umop 路由表（核心 get_conf 同源、按会话绑定）；
        拿不到再按 platform 列表兜底猜（旧版核心/无绑定时）。旧版只按 platform 猜：
        多配置按会话分绑时（如两个好友各一份配置）会全部读到绑平台的那一份——挡位/
        心态段跟着读错对象（11-02 排查：296/507 各绑一份，双方都解析到 507 那份）"""
        if umo in self._conf_umo_cache:
            return self._conf_umo_cache[umo]
        pid = str(umo.split(":")[0])
        cid = "default"
        try:
            mgr = getattr(self.context, "astrbot_config_mgr", None)
            info = mgr.get_conf_info(umo) if mgr else None
            if info:
                got = str(info.get("id") or "")
                if got and got != "default":
                    cid = got
        except Exception:
            pass
        if cid == "default":
            try:
                confs = getattr(self.context.astrbot_config_mgr, "confs", {}) or {}
                for kid, conf in (confs.items() if hasattr(confs, "items") else []):
                    for p in (conf.get("platform") or []):
                        if isinstance(p, dict) and str(p.get("id")) == pid:
                            cid = str(kid)
                            break
                    if cid:
                        break
            except Exception:
                pass
        self._conf_umo_cache[umo] = cid
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
        if getattr(self, "_preset_dirty", False):
            try:
                await self._cfg_obj.save_config_async()
                logger.info("[主动私聊] 用途预设已应用并保存")
            except Exception:
                try:
                    self._cfg_obj.save_config()
                except Exception:
                    logger.warning("[主动私聊] 预设应用后存盘失败（下次重载再生效）", exc_info=True)
            self._preset_dirty = False
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
        self._migrate_state()
        self._rollover_day()

    def _migrate_state(self):
        """v2.5.0 多目标化迁移：晨起/晚安计划、连跳计数、感情温度从单记录改 per-umo/per-qq。
        旧单条归给原第一目标（无目标解析得到就弃，重算/重建无损失）"""
        try:
            tmap = self._target_map() or {}
            flat = [f"{pid}:FriendMessage:{qq}" for pid, qs in tmap.items() for qq in qs]
            first_umo = flat[0] if flat else ""
            first_qq = first_umo.split(":")[-1] if first_umo else ""
            m = self._st.get("morning")
            if isinstance(m, dict) and "key" in m:  # 旧单条 {key,at,sent}
                self._st["morning"] = {first_umo: m} if first_umo else {}
            elif m is None:
                self._st["morning"] = self._st.get("morning") or {}
            n = self._st.get("night")
            if isinstance(n, dict) and "key" in n:
                self._st["night"] = {first_umo: n} if first_umo else {}
            elif n is None:
                self._st["night"] = self._st.get("night") or {}
            for k in ("morning_skip", "evening_skip"):
                v = self._st.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    self._st[k] = {first_qq: v} if first_qq else {}
                elif not isinstance(v, dict):
                    self._st[k] = {}
            # t_warm：旧全局 {rounds,...,t,ts}（判据=有 "t" 键）→ 首次 _warm_t 写入时整体换新，
            # 读侧由 _t_warm_get 兼容兜底，这里不抢跑（目标解析时机不定）
        except Exception:
            logger.warning("[主动私聊] 旧状态迁移失败（按空状态处理）", exc_info=True)

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
            self._st.setdefault("morning_skip", {})
            self._st.setdefault("evening_skip", {})
            self._st.setdefault("night_chat", {})
            self._st.setdefault("seen", {})
            return
        # v2.5.0 连跳计数 per-qq：昨天该号计划已发→清零，没发→+1（无计划的号不动）
        ms = self._st.get("morning_skip") or {}
        ns = self._st.get("evening_skip") or {}
        if not isinstance(ms, dict):
            ms = {}
        if not isinstance(ns, dict):
            ns = {}
        for umo, plan in (self._st.get("morning") or {}).items():
            if isinstance(plan, dict) and plan.get("key"):
                qq = str(umo.split(":")[-1])
                ms[qq] = 0 if plan.get("sent") else int(ms.get(qq, 0)) + 1
        for umo, plan in (self._st.get("night") or {}).items():
            if isinstance(plan, dict) and plan.get("key"):
                qq = str(umo.split(":")[-1])
                ns[qq] = 0 if plan.get("sent") else int(ns.get(qq, 0)) + 1
        self._st["morning_skip"] = ms
        self._st["evening_skip"] = ns
        # 群在场记录滚动：只留 7 天内有活动的目标
        seen = self._st.get("seen") or {}
        cut7 = time.time() - 7 * 86400
        self._st["seen"] = {q: t for q, t in seen.items() if float(t) >= cut7}
        # v2.7.6 缺席留痕例行清理：已提过嘴的（said）留 48h 过期；一直没提的留 72h
        #（她始终没开口提就算了，不再喂感官包）——此前 said 痕会永久躺在状态里
        mt = self._st.get("miss_trace") or {}
        for u in list(mt.keys()):
            _age = time.time() - float(mt[u].get("ts") or 0)
            if (mt[u].get("said") and _age > 48 * 3600) or (not mt[u].get("said") and _age > 72 * 3600):
                del mt[u]
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
        # 心情节律：v2.7.0 方案⑤修正版——AR(1) 正向情绪惯性（φ=0.7 向基线 1.0 回归）+每日
        # 新扰动 ±15%：真人情绪日间自相关为正（Kuppens 情绪惯性研究），昨天高涨今天
        # 更容易偏高、几天内自然淡回平常——不是"昨天密今天必冷"的机械负相关
        mood = 1.0 + 0.7 * (float(self._st.get("mood", 1.0)) - 1.0) + random.uniform(-MOOD_STEP, MOOD_STEP)
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
            "morning": {},   # v2.5.0 per-umo：{umo: {"key","at","sent"}} 晨起计划
            "night": {},     # v2.5.0 per-umo：{umo: {"key","sent"}} 睡前（按夜锚日去重）
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

    @filter.on_llm_request()
    async def on_llm_request_dm(self, event: AstrMessageEvent, req=None):
        """v2.3.0 被动侧事实注入（重逢/小委屈一次性；与 merge 插件注入共存）"""
        try:
            if req is None:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":FriendMessage:" not in umo:
                return
            # v2.6.0 日切种子省缓存：捕获主对话真实系统提示词——种子调用复用同前缀，
            # 50K 历史走缓存价而不是全价（自动跟随平台怎么拼系统头，平台更新零适配）
            _sp = str(getattr(req, "system_prompt", "") or "")
            if _sp:
                if not hasattr(self, "_cap_sys"):
                    self._cap_sys = {}
                self._cap_sys[umo] = _sp
            now = time.time()
            blocks = []
            s = self._sess(umo)
            if s.get("_reunion_due"):
                blocks.append(self._reunion_block(umo, now))
                s["_reunion_due"] = False
            if s.get("sulk_due") and now >= float(s.get("sulk_due") or 0):
                s["sulk_due"] = None
                if s.get("sulk"):
                    blocks.append(self._sulk_block(umo, now))
            if blocks:
                from astrbot.core.agent.message import TextPart as _TP
                req.extra_user_content_parts.append(_TP(text="".join(blocks)).mark_as_temp())
                self._save_state()
        except Exception:
            pass  # 表达层失败绝不影响回复

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """只记账不动消息（任何异常静默放行）。群消息只记目标在场（v2.5.0）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":GroupMessage:" in umo:
                # v2.6.2 群在场三态（用户拍板翻转）：
                # ①@她/回她=跟她说话→seen_chat（想念钟软重置，陪聊中不算分别，绝不吃醋）
                # ②跟别人聊→seen（想念钟照走+掷中×1.4加成+醋口吻——"群里聊得欢都不来找我"）
                sender = str(event.get_sender_id() or "")
                if sender and sender in self._target_qqs():
                    talked = False
                    if _AtComp is not None:
                        try:
                            sid = str(event.get_self_id())
                            talked = any(isinstance(c, _AtComp) and str(getattr(c, "qq", "")) == sid
                                         for c in event.get_messages())
                        except Exception:
                            talked = False
                    key = "seen_chat" if talked else "seen"
                    self._st.setdefault(key, {})[sender] = time.time()
                    # v2.7.0 群@=见面（开关开时）：销约销离席+中签作废——跟 dm_group_at_counts 走
                    if talked and self.c_group_at_counts:
                        self._grp_meet_clear(sender, str(umo.split(":")[0]))
                return
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
            # v2.7.1 指令不污染会话：/ 开头的管理指令不算"他来说话"——不推静默钟、
            # 不触发履约/离席检测（查 /今晚骰子 不该把她"该来了"的钟清掉）
            if self._is_cmd_text(text):
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
            prev_msgs = s.get("msg_times") or []
            # v2.3.0 重逢检测：隔很久再开口 → 置一次性标志，回复时注入委屈事实
            if self.c_reunion_on and prev_msgs and (now - float(prev_msgs[-1])) > 48 * 3600:
                tw = self._t_warm_get(umo)
                if tw.get("t") is not None:
                    s["t_warm_before"] = tw
                s["_reunion_due"] = True
                s["_reunion_gap"] = now - float(prev_msgs[-1])
            # v2.7.3 赴约状态机（设计稿第七轮定稿，替代 2.7.2 的三档版）：
            # ①完成词（通用回归词任意时候/活动完成词严格匹配活动根）→当场赴约；
            # ②假约：dur 类说词后初始链拖超 8 分钟→作废清零无痕（时刻词不适用——
            #   "晚上找你"是宣告今晚的计划，接着聊天不作废）；
            # ③到点/迟到（≥锚-宽限）→赴约；④拜访聊起来（≥3条或跨度>5min）→提前赴约；
            # ⑤锚前冒泡（≤2条的拜访回合，散了没成气候）→约保留、锚冻结（游戏没重开），
            #   冒泡回合计数 bubble_n；⑥第二次冒泡=算回来（没理由一直冒泡耍人）；
            # ⑦还没离开过（说完词连续聊，gap≤5min 且从未有过拜访）→不判赴约，
            #   锚跟最后一条消息走（等效从真离开那刻起算）
            pl = s.get("promise") or {}
            if pl.get("word"):
                _grace = AFK_GRACE_MIN * 60
                gap = now - prev_act
                anchor = self._anchor_of(pl)
                word_ts = float(pl.get("word_ts") or pl.get("event") or prev_act)
                if self._hit_completion(pl["word"], text):
                    self._kept_fulfill(umo, s, pl, now, prev_act, now, via="done")
                elif now >= anchor - _grace:
                    back_ts = float(pl.get("visit_at") or now)
                    dep = float(pl.get("visit_from") or prev_act)
                    self._kept_fulfill(umo, s, pl, back_ts, dep, now, via="ontime")
                elif gap <= _grace:
                    if (not pl.get("visit_ever") and pl.get("kind") != "time"
                            and now - word_ts > FAKE_PROMISE_MIN * 60):
                        s["promise"] = None  # 假约：说完又聊七八分钟的，作废清零无痕
                        self._dlog({"t": "promise_fake", "umo": umo.split(":")[-1], "word": pl["word"]})
                    elif pl.get("visit_at"):  # 拜访中的连续消息：数到聊起来为止
                        pl["visit_n"] = int(pl.get("visit_n") or 1) + 1
                        if int(pl.get("visit_n") or 1) >= 3 or now - float(pl["visit_at"]) > _grace:
                            self._kept_fulfill(umo, s, pl, float(pl["visit_at"]),
                                               float(pl.get("visit_from") or prev_act),
                                               now, via="visit")
                    elif not pl.get("visit_ever"):
                        # 说完约还在聊：没离开过，不判赴约——锚跟最后一条消息走
                        pl["event"] = now
                        if pl.get("kind") != "time":
                            pl["anchor"] = self._make_anchor_ts(pl["word"], now)
                else:  # 离开过、锚前回来
                    if pl.get("visit_at"):  # 上一回合拜访散了没成气候=一次冒泡
                        pl["bubble_n"] = int(pl.get("bubble_n") or 0) + 1
                        pl.pop("visit_at", None)
                        pl.pop("visit_from", None)
                        pl.pop("visit_n", None)
                    if int(pl.get("bubble_n") or 0) >= 1:
                        # 第二次冒泡=算回来
                        self._kept_fulfill(umo, s, pl, now, prev_act, now, via="bubble2")
                    else:
                        pl["visit_at"], pl["visit_from"], pl["visit_n"] = now, prev_act, 1
                        pl["visit_ever"] = True
                        self._dlog({"t": "promise_touch", "umo": umo.split(":")[-1], "word": pl["word"]})
            # v2.7.3 中签撞回归：票不销，顺延 40~80min（只一次）——先顺延、到点自动裁决：
            # 他真聊起来/真回来（赴约清票或 act_ts 越过新锚）→翻篇判定自然作废；
            # 冒泡走人就到点照发（把"人冒头了"这件事消化掉再想他）
            pd = s.get("pending")
            if pd and pd.get("at") and not pd.get("deferred"):
                pd["deferred"] = True
                pd["event"] = now
                pd["at"] = now + random.uniform(DEFER_LO, DEFER_HI) * 60
                self._dlog({"t": "pending_defer", "umo": umo.split(":")[-1],
                            "at": datetime.fromtimestamp(pd["at"]).strftime("%H:%M")})
            # v2.7.0 缺席留痕兜底：销约后当晚他还是来了 → 次日"没等到"的提一嘴作废
            tr = (self._st.get("miss_trace") or {}).get(umo)
            if tr and not tr.get("said") and now - float(tr.get("ts") or 0) < 24 * 3600:
                tr["said"] = True
                self._dlog({"t": "trace_cancel", "umo": umo.split(":")[-1]})
            s["msg_times"] = prev_msgs + [now]
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
            # v2.3.0 回归问候：他回来（任意消息）→ 上一离席锚记学习样本并关闭
            prev_ck = s.get("afk_check") or {}
            if prev_ck.get("word") and not prev_ck.get("done") and not prev_ck.get("learned"):
                gap = now - float(prev_ck.get("event") or 0)
                if 5 * 60 < gap < 16 * 3600:
                    self._st.setdefault("afk_dur_learn", {}).setdefault(prev_ck["word"], []).append(round(gap / 60, 1))
                    lst = self._st["afk_dur_learn"][prev_ck["word"]]
                    if len(lst) > 10:
                        self._st["afk_dur_learn"][prev_ck["word"]] = lst[-10:]
                s["afk_check"] = dict(prev_ck, done=True, learned=True)
            # 离席/约定词：挂在当前静默事件上（事件=act_ts 起算的静默段）
            if self.c_afk_words:
                hit = next((w for w in self.c_afk_words if w in text), None)
                if hit:
                    idx = text.find(hit)
                    pre = text[max(0, idx - 6):idx]
                    # v2.5.5 问句排除：词前紧挨"你"=在问她（"你吃饭了吗"），不是他离席
                    # v2.5.7 自称白名单（替代 2.5.6 人名黑名单——防不完）：词前窗口剔除自称/铺垫词
                    # 和标点语气词后必须什么都不剩才锚——"我去/先/要/马上"解释得掉=他本人，
                    # 剩下任何东西（朋友/小王/他/下楼）=存疑不锚。宁可漏锚不可误锚：
                    # 漏锚落普通冷场档无害；误锚两小时后回归问候演出"TA说吃饭离开"的穿帮
                    residue = pre
                    for f in _AFK_SELF_FILLERS:
                        residue = residue.replace(f, "")
                    residue = re.sub(r"[\s，。！？!?、,.~～了着在吗呢啊哦呀嘛哈吧呗喽~]+", "", residue)
                    if (f"你{hit}" in text) or residue:
                        hit = None
                if hit:
                    # v2.7.5 释放词：同条带"不用等"类 → 释放锚（不开回归窗/档位回落/感官包换文案）
                    _rel = self._release_hit(text)
                    s["afk"] = {"word": hit, "event": now}
                    if _rel:
                        s["afk"]["released"] = True
                        self._dlog({"t": "afk_release", "word": hit, "rel": _rel})
                    # 窗 dict 照建（带 released 永不开窗）：他回来时 afk_dur_learn 样本照记
                    s["afk_check"] = {"word": hit, "event": now, "text": text[:80],
                                      "rounds": self._heat_rounds(umo, now)}
                    if _rel:
                        s["afk_check"]["released"] = True
                        # 释放优先于承诺（跨消息版）：先头挂的约，此刻明说不用等 → 约也放掉，
                        # 不留"到最后也没好好来找我"的留痕——人家发话了
                        if s.get("promise"):
                            s["promise"] = None
                            self._dlog({"t": "promise_release_void", "word": hit})
                    # v2.5.10 离席伴随承诺：同条消息带"找你"（无否定）=约了回来（"吃完找你哟"）——
                    # 事件式前缀不在约定词表（表认时间前缀），漏网会丢回归问候加成档/履约记账/
                    # 被晾委屈的"你明明说找我的"档
                    # v2.7.5 释放优先于承诺（同条版）：都说了不用等，约不挂（矛盾按不用等处理）
                    if (not _rel and "找你" in text and not s.get("promise")
                            and not any(n in text for n in ("不找你", "别找你", "没找你"))):
                        pw = hit + "完找你"
                        s["promise"] = {"word": pw, "event": now, "kind": "dur",
                                        "anchor": self._make_anchor_ts(pw, now),
                                        "word_ts": now, "bubble_n": 0}
            if self.c_promise_words:
                hit = next((w for w in self.c_promise_words if w in text), None)
                # v2.7.5 释放词在场同条不挂约（"晚点找你，你先玩不用等"→只当他走了不用等）
                if hit and not self._release_hit(text):
                    s["promise"] = {"word": hit, "event": now,
                                    "kind": self._promise_kind(hit),
                                    "anchor": self._make_anchor_ts(hit, now),
                                    "word_ts": now, "bubble_n": 0}
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
        """(lo_min, hi_min) 当日分钟。auto=醒来+1h ~ 今晚入睡-30min（夹硬边界；
        床点跨午夜时 hi 落在次日，分钟数+1440 防倒挂）。sleep 读不到→沿用上次
        推导缓存→再退手动值"""
        if self.c_quiet_mode == "manual":
            return _clamp_lo_hi(self.c_quiet_lo, self.c_quiet_hi)
        st = self._sleep_state()
        if st:
            wake_ts = float(st.get("wake_at") or 0)
            bed_ts = self._night_bed_from_state(st, time.time())
            if wake_ts and bed_ts and bed_ts > wake_ts:
                lo = datetime.fromtimestamp(wake_ts + 3600)
                hi = datetime.fromtimestamp(bed_ts - 1800)
                # v2.2.1 修跨午夜倒挂：床点-30min 落到明天（如今晚 00:52 床→00:22）时，
                # 按当日分钟换算 hi=22 会小于 lo → 被硬边界钳成 30 分钟残窗，
                # 静默评估全天在"窗外" silently 关死（11-02 全天零掷即此故）
                lo_min, hi_min = lo.hour * 60 + lo.minute, hi.hour * 60 + hi.minute
                if hi.date() > lo.date():
                    hi_min += 1440
                self._quiet_cache = (lo_min, hi_min)
                return _clamp_lo_hi(lo_min, hi_min)
        if self._quiet_cache:
            return _clamp_lo_hi(*self._quiet_cache)
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
        ts = sorted(t for t in (s.get("msg_times") or []) if win_lo <= t <= last)
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
        # v2.3.0 深度耦合 V：他回应得好（V 高）她少收着，晾她（V 低）矜持加深——
        # 复用 RW 期待值不新开学习器；挡位钳位防学习穿底。
        # v2.3.1 关系底色微调（T）：热恋期被晾一次不会立刻收深（底子厚），冷淡期
        # 同样的冷落分量更重（底子薄）——斜率 0.15=V 的 15%，事件反馈仍主导，
        # T 只垫底色（偏离中心最多 ±7.5%），不翻转任何方向
        a = P.cd_a
        if self.c_cd_a_learn:
            v = float(s.get("exp_v", 0.6))
            tw = self._t_warm_get(umo)
            t_rel = float(tw.get("t") or 0.5) if float(tw.get("ts") or 0) > time.time() - 6 * 3600 else 0.5
            a = min(self.c_cd_a_hi, max(self.c_cd_a_lo,
                   a * (1.0 + self.c_cd_a_slope * (0.6 - v)) * (1.0 + 0.15 * (0.5 - t_rel))))
        f = 1.0 - a * math.exp(-(d - 1) / tau)
        if f >= 0.97:
            return 1.0
        # v2.3.0 当日噪声：每人每日掷一次（独立存 pdrift.date_noise，防"永远精确 0.35"；
        # 独立于 _drift_vals 的开关——drift 关着噪声也要在）
        qq = str(umo.split(":")[-1])
        today = datetime.now().date().isoformat()
        ent = (self._st.get("pdrift") or {}).get(qq) or {}
        noise = float((ent.get("v") or {}).get("date_noise") or 0) \
            if ent.get("date") == today else 0
        if not noise:
            noise = round(random.uniform(0.90, 1.10), 3)
            ent = dict(ent) if ent.get("date") == today else {"date": today, "on": False, "v": {}}
            v = dict(ent.get("v") or {})
            v["date_noise"] = noise
            ent["v"] = v
            if not ent.get("date"):
                ent["date"] = today
            self._st.setdefault("pdrift", {})[qq] = ent
        return round(min(1.0, f * float(noise)), 3)

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
        # v2.7.0 锚感知：约活着（锚未过期）就是约档——锚前她不瞎找（掷骰侧把静默钟
        # 压到 0），锚后到销约前这窗是"该回来了还没回"的念想高峰
        if promise and now < self._anchor_of(promise) + PROMISE_EXPIRE_H * 3600:
            tier = "promise"   # 约定未赴 → 中档
        elif afk and not afk.get("released") and 0 <= ev - float(afk.get("event") or -1) <= grace:
            tier = "afk"       # 离席词 → 吃饭档（v2.7.5 释放锚回落 normal：他说了不用等）
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
            # v2.5.11 有约在身=吃饭也惦记（用户拍板"可以抬"）：6小时内活的约定
            # → 池子下限+0.2（平常0.4~0.7→0.6~0.9）。约了来找的人，等待时的念想
            # 比平常离席浓——与回归问候的约定加成档同源
            pl = s.get("promise") or {}
            if pl.get("word") and 0 < time.time() - float(pl.get("event") or 0) <= 6 * 3600:
                lo, hi = lo + 0.2, min(1.0, hi + 0.2)
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

    def _target_qqs(self) -> set:
        """全部目标 QQ（缓存 60s——群消息高频进来，_target_map 每条跑一遍太重）"""
        now = time.time()
        if not getattr(self, "_tq_cache", None) or now - self._tq_cache[0] > 60:
            try:
                self._tq_cache = (now, {str(q) for qs in (self._target_map() or {}).values() for q in qs})
            except Exception:
                self._tq_cache = (now, set())
        return self._tq_cache[1]

    def _t_warm_get(self, umo: str) -> dict:
        """v2.5.0 per-umo 读感情温度：新结构 {umo: {...}}；旧全局单记录（有 "t" 键）
        兼容归给第一目标，其余目标拿到空 dict（消费侧按缺省 0.5 处理）"""
        tw = self._st.get("t_warm") or {}
        ent = tw.get(umo)
        if isinstance(ent, dict) and "t" in ent:
            return ent
        if "t" in tw:  # 旧全局单条
            try:
                flat = [str(q) for qs in (self._target_map() or {}).values() for q in qs]
                if flat and str(umo.split(":")[-1]) == flat[0]:
                    return tw
            except Exception:
                pass
        return {}

    def _target_umos(self) -> list:
        """全部目标的 umo 平铺（晨起/晚安 per-umo 计划用）"""
        try:
            return [f"{pid}:FriendMessage:{qq}" for pid, qs in (self._target_map() or {}).items() for qq in qs]
        except Exception:
            return []

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
        # 补发通道：管理员显式意志，跳过活跃终检强制发（仍计总量）。
        # v2.5.0 A4：可选 "qq" 字段定向补发（不填=全员广播，兼容旧文件）
        mk = self._consume_makeup()
        if mk:
            tpl = {
                "night": self.tpl_makeup_night,
                "morning": self.tpl_morning,
                "random": self.tpl_random,
            }.get(mk.get("kind"), self.tpl_random)
            await self._fire("makeup", tpl, only_qq=mk.get("qq") or None)
        self._rollover_day()
        now = time.time()
        phase = self._sleep_phase()
        sl = self._sleep_state() or {}

        # ---------- v2.6.0 日切（醒来开新对话+昨日种子；必须在晨起/留言回应之前） ----------
        if self.c_day_cut:
            await self._day_cut_eval(now, phase)

        # ---------- 晨起（v2.5.0 per-umo：每个目标自己的计划/掷骰/发送；留言回应线） ----------
        if self.c_morning:
            wake_ts = float(sl.get("wake_at") or 0)
            plans = self._st.get("morning") or {}
            if wake_ts and now - wake_ts < 6 * 3600:
                key = datetime.fromtimestamp(wake_ts).date().isoformat()
                new_plan = False
                for umo in self._target_umos():
                    m = plans.get(umo)
                    if not m or m.get("key") != key:
                        shift = random.uniform(30, 60) * 60 if self._night_owl_level() == "owl" else 0
                        plans[umo] = {"key": key, "at": wake_ts + random.uniform(5, 30) * 60 + shift,
                                      "sent": False}
                        new_plan = True
                if new_plan:
                    self._st["morning"] = plans
                    self._save_state()
            for umo, m in list(plans.items()):
                if not m.get("sent") and now >= m.get("at", 0):
                    if phase == "temp_wake":
                        m["at"] = now + 10 * 60
                        self._save_state()
                    elif phase == "awake":
                        m["tries"] = int(m.get("tries", 0)) + 1
                        await self._morning_decide(umo, m, wake_ts, now)
                        self._save_state()

        # ---------- 睡前（v2.5.0 per-umo：今晚聊过必收尾/已道晚安换 night2 各自独立判） ----------
        if self.c_night:
            bed_ts = self._tonight_bed_ts(sl, now)
            if bed_ts:
                key = datetime.fromtimestamp(bed_ts).date().isoformat()
                nplans = self._st.get("night") or {}
                new_plan = False
                for umo in self._target_umos():
                    n = nplans.get(umo)
                    if not n or n.get("key") != key:
                        nplans[umo] = {"key": key, "sent": False}
                        new_plan = True
                if new_plan:
                    self._st["night"] = nplans
                    self._save_state()
                for umo, n in list(nplans.items()):
                    if not n.get("sent") and phase == "awake" and 0 < bed_ts - now <= NIGHT_WINDOW_MIN * 60 + 60:
                        n["tries"] = int(n.get("tries", 0)) + 1
                        await self._night_decide(umo, n, bed_ts, now)
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

        # ---------- v2.3.0 回归问候（离席到点未归） ----------
        if self.c_afk_check_on:
            tmap = self._target_map()
            for _pid, _qs in (tmap or {}).items():
                for _qq in _qs:
                    await self._afk_check_eval(f"{_pid}:FriendMessage:{_qq}", now, phase)

        # ---------- 懒结算：待观察超 12h 按没回处理 ----------
        for umo, s in (self._st.get("sess") or {}).items():
            pr = s.get("pending_reply")
            if pr and now - float(pr.get("sent_at") or 0) > self.c_reply_slow * 60:
                s["pending_reply"] = None
                self._rw_update(umo, self._P(umo).rw_r_none)
            # v2.3.0 promise 未赴：约定超 6h 没兑现且他没来 → kept=False（喂 T 履约率）
            _pl = (s.get("promise") or {})
            if _pl.get("word") and now - float(_pl.get("event") or 0) > 6 * 3600:
                _lg = (s.setdefault("promise_log", []) or [])[-10:]
                _lg.append({"word": _pl["word"], "at": float(_pl.get("event") or 0), "kept": False})
                s["promise_log"] = _lg[-10:]
                s["promise"] = None
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

    def _sleep_pending_of(self, umo: str, sl: dict) -> list:
        """v2.5.0 E：读睡觉插件的夜间缓冲（该目标睡着期间发来的留言）。只读零污染；
        sleep 未装/无状态/没缓冲 → 空表。buffers 条目 {ts,name,text}"""
        try:
            buf = (sl or {}).get("buffers") or {}
            ent = buf.get(umo)
            return [e for e in (ent or []) if isinstance(e, dict) and str(e.get("text") or "").strip()]
        except Exception:
            return []

    def _mark_replied(self, umo: str):
        """v2.5.0 E：留言回应线发送后写标记文件到睡觉插件数据目录（makeup.json 同款
        文件通信）——sleep 冲刷前见标记跳过注入，防"她回完留言、冲刷轮再演一遍翻未读" """
        try:
            p = self._data_dir.parent / "astrbot_plugin_dafeiyu_sleep" / "dm_morning_reply.json"
            data = {}
            try:
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            today = datetime.now().date().isoformat()
            if data.get("date") != today:
                data = {"date": today, "umos": []}
            if umo not in data["umos"]:
                data["umos"].append(umo)
            p.parent.mkdir(parents=True, exist_ok=True)  # sleep 目录可能不存在（新装/迁移）——建了再写
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.info("[主动私聊] 留言回应标记文件写入失败（sleep 侧会重复冲刷，无功能损害）")

    def _flushed_today(self, umo: str, sl: dict) -> bool:
        """该目标今天的晨间冲刷是否已发生（sleep 的 morning_flushed 标记）"""
        mf = (sl or {}).get("morning_flushed") or {}
        return mf.get("date") == datetime.now().date().isoformat() and umo in (mf.get("umos") or [])

    async def _morning_reply_send(self, umo: str, msgs: list, now: float) -> bool:
        """v2.5.0 E 留言回应线：醒后必发（p=1.0）一条"刚醒看到留言"。必发=不走 AI 否决；
        发送前再查一次冲刷标记（他先开口=冲刷已带出内容，互斥作废）"""
        sl = self._sleep_state() or {}
        if self._flushed_today(umo, sl):
            self._dlog({"t": "morning_reply_void", "why": "他已先开口,冲刷已带出"})
            return True  # 销账：让位给冲刷
        if not self._send_open(umo, now):
            return False  # D7 发送熔断中：退避后窗口再看
        lines = []
        for m in msgs[:5]:
            hm = datetime.fromtimestamp(float(m.get("ts") or now)).strftime("%H:%M")
            lines.append(f"[{hm}] {str(m.get('text') or '')[:60]}")
        ctx = self._status_ctx(umo) + self._stance_line(umo) + (
            f"[TA在你睡着时给你发了 {len(msgs)} 条留言（刚醒来才看到）：\n" + "\n".join(lines) + "]\n"
        )
        # v2.5.1 sleep 0.8.1 联动：昨晚聊着聊着睡着（无声溜走标记在）→ 回应里自然提一句
        ss = sl.get("silent_slip") or {}
        if ss.get("umos") and umo in (ss.get("umos") or []):
            ctx += "（另外你是昨晚聊着聊着直接睡着的，没来得及道别——回应里可以自然带一句，提一次就好别过度道歉）\n"
        text = await self._gen(self._morning_reply_tpl(umo), umo=umo, kind="morning_reply",
                               veto_ok=False, veto_ctx=ctx)
        if text is None:
            text = random.choice(self.c_pool)  # 必发：生成失败回落语料池也不许哑
        try:
            await self._send_human(umo, text)
        except Exception:
            logger.warning(f"[主动私聊] 留言回应发送失败 {umo}", exc_info=True)
            return False  # 不销账，下轮 tick 再试（at 顺延）
        # 留言原文随她的回应一并归档（冲刷被跳过后，历史里也得有这笔账）
        archive_text = text + (f"\n[本轮工具：{'；'.join(self._tool_trace[:3])}]" if self._tool_trace else "")
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                await cm.add_message_pair(
                    cid=cid,
                    user_message={"role": "user", "content": "[系统标记：TA睡着时发来上述留言，你刚醒主动回应了]" + "\n" + "\n".join(lines)},
                    assistant_message={"role": "assistant", "content": archive_text},
                )
        except Exception:
            logger.warning("[主动私聊] 留言回应归档失败（不影响发送）", exc_info=True)
        self._mark_replied(umo)
        # 配额照计（正经主动消息）：总量/间隔/每人计数；不计 unans（这是回应他，不是主动搭话）
        self._st["sent_total"] = int(self._st.get("sent_total", 0)) + 1
        self._st["last_dm_ts"] = now
        qq = str(umo.split(":")[-1])
        if self.c_tier_per_conf:
            d = self._st.setdefault("per", {}).setdefault(qq, {})
            d["sent"] = int(d.get("sent", 0)) + 1
        self._dlog({"t": "morning_reply", "gap_min": round((now - float(self._sleep_state().get("wake_at") or now)) / 60, 1),
                    "n_msgs": len(msgs), "text": text[:30]})
        return True

    async def _morning_decide(self, umo: str, m: dict, wake_ts: float, now: float):
        """晨起决策（v2.5.0 per-umo）：睡时留言→必发回应线（替代掷骰）；他先开口→让位；
        锚点必发；概率分支掷骰。原"存稿让位冲刷"分支已删（被回应线替代）"""
        s = self._sess(umo)
        sl = self._sleep_state() or {}
        # 他醒后已先开口 → 冲刷已带出晨间内容，不重复报到
        if wake_ts and await self._user_spoke_after(umo, wake_ts):
            m["sent"] = True
            self._dlog({"t": "morning_skip", "why": "用户已先开口", "umo": umo.split(":")[-1]})
            return
        # v2.5.0 E：睡着时有他的留言 → 不掷骰，醒后必发回应（回应即报到）
        pend = self._sleep_pending_of(umo, sl)
        if pend:
            ok = await self._morning_reply_send(umo, pend, now)
            if ok:
                m["sent"] = True
                self._st.setdefault("morning_skip", {})[str(umo.split(":")[-1])] = 0
            elif int(m.get("tries", 0)) >= 8:
                m["sent"] = True  # 重试上限：放弃（防发送持续失败时空转生成）
            else:
                m["at"] = now + 5 * 60  # 发送失败（终检/网络）：5分钟后再看
            return
        qq = str(umo.split(":")[-1])
        anchor = int((self._st.get("morning_skip") or {}).get(qq, 0)) >= 2  # 连跳2天→今日必发
        if not m.get("rolled"):
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
                    self._st.setdefault("morning_skip", {})[qq] = 0
            else:
                m["at"] = now + 5 * 60  # 终检/近距查重顺延，5分钟后再看
        else:
            m["sent"] = True  # 没中：今日作废（锚点已强制 win=True）

    async def _night_decide(self, umo: str, n: dict, bed_ts: float, now: float):
        """睡前决策（v2.5.0 per-umo）：今晚聊过→必收尾；没聊→概率×夜聊档；锚点必发；
        已道晚安→换收尾模板——各目标会话独立判"""
        s = self._sess(umo)
        qq = str(umo.split(":")[-1])
        chatted = len([t for t in (s.get("msg_times") or []) if bed_ts - 2 * 3600 <= t <= now]) > 0
        # 你先道过别（同晚8小时内）→ 即使在她睡前2小时窗口外，躺下也必收尾：
        # 道别是对等的，不该因为她自己睡晚了就漏掉你说的晚安
        gn = float(s.get("goodnight_ts") or 0)
        said_bye = bed_ts - 8 * 3600 <= gn <= now
        if said_bye:
            chatted = True
        already = await self._already_said_goodnight(umo)
        anchor = int((self._st.get("evening_skip") or {}).get(qq, 0)) >= 2
        if not n.get("rolled"):
            owl_f = {"owl": 1.2, "mid": 1.0, "early": 0.7}[self._night_owl_level()]
            p = self._P(umo).evening_base * owl_f
            n["rolled"] = True
            n["p"] = round(p, 3)
            n["win"] = chatted or anchor or (random.random() < p)
            self._dlog({"t": "night_roll", "p": round(p, 3), "chatted": chatted, "said_bye": said_bye,
                        "owl_f": owl_f, "anchor": anchor, "win": n["win"],
                        "umo": umo.split(":")[-1]})  # v2.7.6 带目标：多目标时晚安归谁可审计
        if n.get("win"):
            # v2.7.8 顺位给睡眠：sleep 陪聊宽限正陪着这个会话→收尾归 sleep 的困意/嘟囔线，
            # dm 今晚销账不插话（她真走那刻的收尾比排程点的晚安真，也不再"先道晚安又聊半小时"）
            grace = (self._sleep_state() or {}).get("grace")
            if isinstance(grace, dict) and str(grace.get("last_umo") or "") == umo:
                n["sent"] = True
                self._st.setdefault("evening_skip", {})[qq] = 0
                self._dlog({"t": "night_yield", "why": "sleep宽限接管收尾",
                            "umo": umo.split(":")[-1]})
                return
            # v2.7.8 已互道晚安：她最近在对话里道过 且 他60分钟内也道过别——收尾已在
            # 对话里完成，销账不发（再发=又一句晚安）；陈年晚安不算，仍走报备款收尾
            if already and gn and 0 < now - gn <= 60 * 60:
                n["sent"] = True
                self._st.setdefault("evening_skip", {})[qq] = 0
                self._dlog({"t": "night_skip", "why": "已互道晚安(60min内)",
                            "umo": umo.split(":")[-1]})
                return
            tpl = self.tpl_night2 if already else self.tpl_night
            force_at_bed = bed_ts - now <= 60 * 60  # 到睡觉线1小时内：强制发（活跃终检/查重豁免）
            # v2.7.6 必收尾（聊过/你先道别/连跳锚）一并豁免否决——作息事件不交"该不该打扰"的
            # 判断：她马上要沉默八小时，收尾句严格优于无声消失；概率掷中的晚安照旧可拦
            sent = await self._send_greet("night", umo, tpl, now, force=force_at_bed,
                                          no_veto=bool(chatted or anchor))
            if sent or n["tries"] >= 6:
                n["sent"] = True
                if sent:
                    self._st.setdefault("evening_skip", {})[qq] = 0
            else:
                n["tries2"] = int(n.get("tries2") or 0) + 1
                n["at_hint"] = now + 5 * 60  # 顺延5分钟；窗口逻辑下轮 tick 自然再看
        else:
            n["sent"] = True

    async def _send_greet(self, kind: str, umo: str, template: str, now: float, force: bool = False,
                          no_veto: bool = False) -> bool:
        """早晚安发送：活跃终检（force=到睡觉线豁免）→ 近距查重 → 否决/生成 → 拟人发送+记账
        no_veto=必发语义线（睡前必收尾/锚点必发）：豁免否决层——v2.7.6 修"必收尾被 AI 否决
        后销账=整晚零收尾"（他先道了晚安她却无声睡去）"""
        s = self._sess(umo)
        if not self._send_open(umo, now):
            return False  # D7 发送熔断中：本轮不发（不销账，退避结束后窗口再看）
        # v2.7.8 活跃终检不再被强制窗豁免：热聊中硬插晚安=插话（11-08 00:16事故）。
        # 强制窗改只豁免近距查重——必达性由窗口循环重试保证（你安静后自然发出）
        if self.c_active_check and now - float(s.get("act_ts") or 0) < self.c_active_check * 60:
            return False  # 你正在聊：顺延（含强制窗）
        if not force and self.c_greet_gap and now - float(self._st.get("last_dm_ts") or 0) < self.c_greet_gap * 60:
            return False  # 刚发过别的主动：近距查重（强制窗豁免——睡前收尾优先）
        text = await self._gen(template, umo=umo, kind=kind, veto_ok=self.c_ai_veto and not no_veto,
                               veto_ctx=self._status_ctx(umo) + self._stance_line(umo))
        if text is None:
            self._dlog({"t": f"{kind}_veto", "why": "AI否决", "umo": umo.split(":")[-1]})
            return True  # 否决=今日销账（仅概率分支会到这——no_veto 线生成失败回落语料池不会None）
        try:
            await self._send_human(umo, text)
        except Exception:
            logger.warning(f"[主动私聊] {kind} 发送失败 {umo}", exc_info=True)
            return False
        archive_text = text + (f"\n[本轮工具：{'；'.join(self._tool_trace[:3])}]" if self._tool_trace else "")
        await self._archive_dm(umo, archive_text)
        self._bookkeep(umo, now, kind)
        self._dlog({"t": f"{kind}_sent", "text": text[:30], "umo": umo.split(":")[-1]})
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
                # v2.7.4 离席锚三段保质期（治"前天的吃饭喂到今天"）：新鲜期（预期时长内）
                # 照旧"去吃饭还没回"；过点期（到预期×2.5，封顶8h）改喂"说了去吃饭都X小时
                # 没冒头"——调侃燃料（一顿饭吃三小时？），不当真还在吃；超时彻底不提
                # v2.7.5a 回来即闭嘴：锚说"还没回"，可他锚后出过声（act 超过词后宽限）——
                # 叙事已死，不再注入（治"人回来聊过了她还念3小时没冒头"的穿帮）
                _a_ev = float(afk.get("event") or act)
                if act <= _a_ev + AFK_GRACE_MIN * 60:
                    _a_dur = self._afk_dur_learn_min(afk["word"])
                    _a_age = (now - _a_ev) / 60.0
                    if afk.get("released"):
                        # v2.7.5 释放锚：他交代了不用等——她不掐表，惦记轻着来
                        if _a_age <= _a_dur:
                            anchor = ("，TA {} 说「{}」还交代了不用等——他自己说要一阵子，"
                                      "你别掐着表，想说话就随便念叨一句（知道他在忙），不用等他回").format(
                                datetime.fromtimestamp(_a_ev).strftime("%H:%M"), afk["word"])
                        elif _a_age <= min(_a_dur * 2.5, 480):
                            anchor = ("，TA {} 说「{}」（交代过不用等），到现在 {} 小时没冒头——"
                                      "按说早完事了，人家都发话不用等，惦记的话轻轻带一句就行").format(
                                datetime.fromtimestamp(_a_ev).strftime("%H:%M"), afk["word"], round(_a_age / 60))
                    elif _a_age <= _a_dur:
                        anchor = "，TA {} 说「{}」离开还没回".format(
                            datetime.fromtimestamp(_a_ev).strftime("%H:%M"), afk["word"])
                    elif _a_age <= min(_a_dur * 2.5, 480):
                        anchor = ("，TA {} 说「{}」离开，到现在 {} 小时没冒头——早该完事了，"
                                  "可以纳闷或调侃一句（一顿饭吃三小时？那种味），别当出了什么事").format(
                            datetime.fromtimestamp(_a_ev).strftime("%H:%M"), afk["word"], round(_a_age / 60))
            promise = s.get("promise") or {}
            if isinstance(promise, dict) and promise.get("word"):
                pa = self._anchor_of(promise)
                _pmin = int((pa - now) // 60)
                if _pmin > 5:
                    anchor += "，约过 {}「{}」还没兑现——按说 {} 前后该忙完来找你（还有约 {} 分钟）".format(
                        datetime.fromtimestamp(float(promise.get("event") or act)).strftime("%H:%M"),
                        promise["word"], datetime.fromtimestamp(pa).strftime("%H:%M"), _pmin)
                elif _pmin >= -PROMISE_EXPIRE_H * 60:
                    anchor += "，约过 {}「{}」——按说这会儿刚忙完，该回来了".format(
                        datetime.fromtimestamp(float(promise.get("event") or act)).strftime("%H:%M"),
                        promise["word"])
                else:
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
        # v2.3.0 生成端 T 事实：关系温度三档（措辞自然带温度；概率侧另有接入）
        tw = self._t_warm_get(umo)
        tw_ts = float(tw.get("ts") or 0)
        if tw_ts and time.time() - tw_ts < 6 * 3600:
            tv = float(tw.get("t") or 0.5)
            mood_w = "热乎" if tv > 0.65 else ("有点淡" if tv < 0.35 else "平常")
            parts.append(f"最近你们聊得{mood_w}")
        # v2.5.0 睡觉插件困倦感知（sleep v0.8 联动，软耦合：字段缺席=无行）
        dl = int((self._sleep_state() or {}).get("drowsy_level") or 0)
        if dl >= 2:
            parts.append("你现在很困（话少、反应慢，困得有点撑不住）")
        elif dl == 1:
            parts.append("你现在有点困")
        # v2.7.0 缺席留痕（抗规律感①）：几小时前的约没等到人、她也没去找他 → 感官包
        # 带上这一嘴的事实，她开口自然提（轻轻的，不兴师问罪）；他后来回来了/她提过了就不再带
        tr = (self._st.get("miss_trace") or {}).get(umo)
        if tr and not tr.get("said") and now - float(tr.get("ts") or 0) > 6 * 3600:
            _td = (now - float(tr.get("ts") or 0)) / 3600
            _when = "刚才" if _td < 14 else ("昨天" if _td < 36 else "前两天")
            parts.append(f"{_when}TA说「{tr.get('word')}」到最后也没好好来找我，你也没等到——"
                         "现在开口可以自然带一嘴这事，轻轻的，不兴师问罪")
        parts.append(f"今日已主动 {int(self._st.get('sent_total', 0))} 条；现在 {datetime.now().strftime('%H:%M')}")
        return "[背景事实：" + "；".join(parts) + "]\n"

    def _heat_span(self, umo: str, now: float) -> int:
        """热度窗口内首末消息的跨度（分钟）——画像行"聊了多久"用"""
        s = self._sess(umo)
        last = float(s.get("act_ts") or 0)
        if not last:
            return 0
        win_lo = last - self.c_heat_win * 60
        ts = sorted(t for t in (s.get("msg_times") or []) if win_lo <= t <= last)
        return int((ts[-1] - ts[0]) // 60) if len(ts) >= 2 else 0

    # ---------- v2.3.0 感情温度 T（长期关系面；快照/趋势/V/T 四张脸防重账总纲） ----------

    @staticmethod
    def _sat(x: float) -> float:
        """比值→[0,1] 饱和映射：≥1.5→1.0、≤0.3→0.0 线性夹中间（跟自己基线比）"""
        return min(1.0, max(0.0, (x - 0.3) / 1.2))

    def _warm_t(self, umo: str) -> float:
        """T=0.4×轮数趋势+0.3×联系频率+0.3×约定履约率（各路缺数据按 0.5）。
        开窗/开注入时算一次存 t_warm；msg_times 只存 26h，不足 14 天就有什么用什么——
        信号面变窄但方向仍对（新关系冷启动由缺路回 0.5 兜）"""
        s = self._sess(umo)
        now = time.time()
        ts = [float(x) for x in (s.get("msg_times") or []) if float(x) > 0]
        r_rounds = r_freq = keep = None
        if len(ts) >= 4:
            cut14 = now - 14 * 86400
            base_ts = sorted(x for x in ts if x >= cut14)
            if len(base_ts) >= 4:
                # 联系频率：近7天日均 ÷ 14天日均
                cut7 = now - 7 * 86400
                n7 = len([x for x in base_ts if x >= cut7])
                days7 = max(1.0, (now - cut7) / 86400) if base_ts[-1] < cut7 else max(
                    1.0, min(7.0, (now - base_ts[0]) / 86400))
                span_all = max(1.0, min(14.0, (now - base_ts[0]) / 86400))
                freq7, freq_all = n7 / days7, len(base_ts) / span_all
                if freq_all > 0:
                    r_freq = self._sat(freq7 / freq_all)
                # 轮数趋势：近7天每场轮数中位 ÷ 14天每场轮数中位（burst_gap 聚合）
                def _rounds_med(lo_t):
                    win = [x for x in base_ts if x >= lo_t]
                    if len(win) < 2:
                        return None
                    ses, prev = [], win[0]
                    cnt = 1
                    for x in win[1:]:
                        if x - prev > self.c_burst_gap:
                            ses.append(cnt); cnt = 1
                        else:
                            cnt += 1
                        prev = x
                    ses.append(cnt)
                    ses.sort()
                    return ses[len(ses) // 2]
                m7, m_all = _rounds_med(cut7), _rounds_med(cut14)
                if m7 and m_all:
                    r_rounds = self._sat(m7 / m_all)
        # 约定履约率：最近 10 次 promise 锚实际回来占比（回来=promise 后 6h 内有他消息）
        log = (s.get("promise_log") or [])[-10:]
        if len(log) >= 3:
            keep = sum(1 for e in log if e.get("kept")) / len(log)
        t = (0.4 * (0.5 if r_rounds is None else r_rounds)
             + 0.3 * (0.5 if r_freq is None else r_freq)
             + 0.3 * (0.5 if keep is None else keep))
        t = round(min(1.0, max(0.0, t)), 3)
        tw = self._st.get("t_warm")
        if not isinstance(tw, dict) or "t" in tw:  # 旧全局单条 → per-umo 换新
            tw = self._st["t_warm"] = {}
        tw[umo] = {"rounds": r_rounds, "freq": r_freq, "keep": keep, "t": t,
                   "ts": time.time(), "at": datetime.now().strftime("%m-%d %H:%M")}
        return t

    # ---------- v2.7.0 约的锚点（三分类：时刻词/活动词/模糊词） ----------

    def _promise_kind(self, word: str) -> str:
        """时刻词→time（锚=今晚钟点，冒泡不挪窝）；其余→dur（锚=说话时刻+预期时长）"""
        w = str(word)
        return "time" if any(w.startswith(t) or t in w[:3] for t in PROMISE_TIME_WORDS) else "dur"

    def _promise_prior_min(self, word: str) -> float:
        """该约词的先验时长（分钟）：配置覆盖 > 活动词先验（"打游戏完找你"→150）
        > 模糊词梯度（等会45/晚点90/回头150）> 90 兜底"""
        w = str(word)
        if w in self.c_afk_dur_map:
            return float(self.c_afk_dur_map[w])
        if w.endswith("完找你") and w[:-3] in AFK_DUR_PRIOR:
            return float(AFK_DUR_PRIOR[w[:-3]])
        b = w[:-2] if w.endswith("找你") else w
        for k in (b, b[:2], b[:3]):
            if k in VAGUE_DUR_PRIOR:
                return float(VAGUE_DUR_PRIOR[k])
        return 90.0

    def _promise_dur_min(self, word: str) -> float:
        """预期等待时长（分钟）——抗规律感②锚取分布不取中位数：学习样本≥3 直接
        从他本人真实历史里抽一张（方差是他自己的，不是人造噪声）；冷启动先验
        ×0.75~1.35 抖动防可数规律。抗规律感③心情挪锚：高涨提前/蔫了推后（只动时机）"""
        w = str(word)
        ent = sorted(float(x) for x in ((self._st.get("afk_dur_learn") or {}).get(w) or []) if x > 0)
        if len(ent) >= 3:
            dur = random.choice(ent[-10:])
        else:
            dur = self._promise_prior_min(w) * random.uniform(0.75, 1.35)
        mood = float(self._st.get("mood", 1.0))
        if mood >= ANCHOR_MOOD_HI:
            dur *= ANCHOR_MOOD_FAST
        elif mood <= ANCHOR_MOOD_LO:
            dur *= ANCHOR_MOOD_SLOW
        return max(20.0, dur)

    def _make_anchor_ts(self, word: str, base_ts: float) -> float:
        """锚点三分类→绝对时刻：时刻词锚今晚 19:00~20:00 随机一分钟（说词时已是
        晚上→30~60min 后头上）；活动/模糊词锚 base_ts+预期时长抽样"""
        if self._promise_kind(word) == "time":
            dt = datetime.fromtimestamp(base_ts)
            target = (dt.replace(hour=0, minute=0, second=0, microsecond=0)
                      + timedelta(minutes=random.randint(*PROMISE_TIME_WIN))).timestamp()
            if target < base_ts + 20 * 60:  # 已经是晚上了：锚到半小时后头上
                target = base_ts + random.randint(30, 60) * 60
            return self._anchor_cap(target, base_ts)
        return self._anchor_cap(base_ts + self._promise_dur_min(word) * 60, base_ts)

    def _anchor_of(self, p: dict) -> float:
        """取约的有效锚；2.6.x 旧状态没有 anchor 字段 → 按词先验分布懒补"""
        a = float(p.get("anchor") or 0)
        if not a:
            a = self._make_anchor_ts(str(p.get("word") or "晚点找你"),
                                     float(p.get("event") or time.time()))
            p["anchor"] = a
        return a

    def _anchor_cap(self, anchor: float, base_ts: float) -> float:
        """v2.7.3 夜约钳窗尾（穿窗否决）：锚不越今晚评估窗尾-15min——她当晚临睡前还
        来得及想一把，而不是只能等次晨留痕抱怨。下限=说词+20min，再晚挂的约不强求"""
        try:
            _, qhi = self._quiet_window()
            bd = datetime.fromtimestamp(base_ts)   # 窗尾按"说词当天"算——锚跨午夜不能用锚自己的日期
            cap = bd.replace(hour=int(qhi) // 60, minute=int(qhi) % 60,
                             second=0, microsecond=0).timestamp() - ANCHOR_CAP_MARGIN * 60
        except Exception:
            return anchor
        if anchor <= cap:
            return anchor
        return max(cap, base_ts + 20 * 60)

    def _hit_completion(self, word: str, text: str) -> bool:
        """v2.7.3 完成词=当场赴约：通用回归词任意命中；活动完成词严格匹配约词的
        活动根（"打完游戏了"销游戏约、"吃完饭啦"销不动游戏约）——根首字+"完"与
        根其余部分同现才算，否定（"还没打完"）不算"""
        t = str(text)
        if any(w in t for w in GENERIC_RETURN_WORDS):
            return True
        root = str(word)[:-3] if str(word).endswith("完找你") else ""
        if len(root) >= 2:
            if f"{root[0]}完" in t and root[1:] in t and f"没{root[0]}完" not in t:
                return True
        return False

    def _kept_fulfill(self, umo: str, s: dict, pl: dict, back_ts: float, dep: float,
                      now: float, via: str):
        """v2.7.3 赴约统一收口：记履约（kept 恒记——人到了就是到了）+ 样本三闸
        （真离开>5min / 卫生<先验×2.5 / 过早<预期3/5不入池，连续第4次起入池，
        正常时长即清零计数）+ 销约销票。样本=出发→他回来第一条（从最后一条消息起算）"""
        w = str(pl.get("word") or "")
        anchor = self._anchor_of(pl)
        real_gap = back_ts - dep
        expected = max(20.0, (anchor - dep) / 60.0)
        elapsed = real_gap / 60.0
        grace = AFK_GRACE_MIN * 60
        early = real_gap > grace and elapsed < expected * EARLY_RATIO
        log = (s.setdefault("promise_log", []) or [])[-10:]
        log.append({"word": w, "at": now, "kept": True})
        s["promise_log"] = log[-10:]
        pooled = False
        if grace < real_gap < self._promise_prior_min(w) * 2.5 * 60:
            streaks = self._st.setdefault("early_streak", {})
            if not early:
                streaks[w] = 0
                self._st.setdefault("afk_dur_learn", {}).setdefault(w, []).append(round(elapsed, 1))
                pooled = True
            else:
                streaks[w] = int(streaks.get(w, 0)) + 1
                if streaks[w] >= EARLY_STREAK_N:
                    self._st.setdefault("afk_dur_learn", {}).setdefault(w, []).append(round(elapsed, 1))
                    pooled = True
            lst = self._st.get("afk_dur_learn", {}).get(w) or []
            if 0 < len(lst) > 10:
                self._st["afk_dur_learn"][w] = lst[-10:]
        s["promise"] = None
        if s.get("pending"):   # 人都到了，中签的票不用留着
            s["pending"] = None
            self._dice_done(umo)
        self._dlog({"t": "promise_kept", "umo": umo.split(":")[-1], "word": w,
                    "gap_min": round(elapsed, 1), "early_ratio": round(elapsed / expected, 2),
                    "via": via, "pooled": pooled})

    # ---------- v2.3.0 回归问候 ----------

    def _release_hit(self, text: str):
        """v2.7.5 释放词命中（"不用等"类）：只在已锚到离席词的同条消息里被调用；
        短串子串匹配，天然盖住"不用等我啦/哈"等尾缀"""
        return next((w for w in self.c_release_words if w in text), None)

    def _afk_dur_learn_min(self, word: str) -> float:
        """该词的预期时长（分钟）：学习表 n≥3 用中位数（偶数取中间均值），否则先验；配置覆盖最优先"""
        if word in self.c_afk_dur_map:
            return float(self.c_afk_dur_map[word])
        ent = ((self._st.get("afk_dur_learn") or {}).get(word)) or []
        ent = sorted(float(x) for x in ent if float(x) > 0)
        if len(ent) >= 3:
            n = len(ent)
            return (ent[n // 2] if n % 2 else (ent[n // 2 - 1] + ent[n // 2]) / 2.0)
        return float(AFK_DUR_PRIOR.get(word, 90))

    def _afk_check_anchor(self, umo: str, now: float):
        """离席锚是否应开回归窗：锚有效、窗已到点、窗未过宽、他没再说话。
        返回 (锚dict, intent, 窗开始时刻) 或 None"""
        s = self._sess(umo)
        a = s.get("afk_check") or {}
        # v2.7.5 释放锚永不开窗：dict 留着只当学习载体（他回来记 afk_dur_learn 样本）
        if not a.get("word") or a.get("done") or a.get("released"):
            return None
        ev = float(a.get("event") or 0)
        if not ev or float(s.get("act_ts") or 0) > ev:
            return None  # 他已回来（说过话），窗作废
        win_start = float(a.get("win_at") or 0)
        if not win_start:
            dur = self._afk_dur_learn_min(a["word"]) * 60
            win_start = ev + dur + random.uniform(15, 45) * 60
            # 夜间不开窗：窗起点落在 23:30~08:00 顺延到次日 09:00（免深夜追问）
            ws_dt = datetime.fromtimestamp(win_start)
            m = ws_dt.hour * 60 + ws_dt.minute
            if m >= 23 * 60 + 30 or m < 8 * 60:
                nxt = ws_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                if m >= 23 * 60 + 30:
                    nxt = nxt + timedelta(days=1)
                win_start = nxt.timestamp()
            a["win_at"] = win_start
            a["f_intent"] = None  # 池值开窗时掷、窗内锁定
            self._save_state()
        if now < win_start or now > win_start + AFK_CHECK_WIN_MIN * 60:
            if now > win_start + AFK_CHECK_WIN_MIN * 60:
                a["done"] = True  # 窗过了没掷中：此锚翻篇，不追问第二遍
                self._save_state()
            return None
        return a

    def _afk_intent(self, umo: str, a: dict):
        """意图分层（优先级 拒绝>约定>告别>普通）。返回 (intent, f值或None)；拒绝返回 (None,0)"""
        s = self._sess(umo)
        now = time.time()
        # 拒绝词：锚消息原文或其后 5min 内他的消息命中 → 本窗不开
        if self.c_refuse_words:
            said = str(a.get("text") or "")
            for w in self.c_refuse_words:
                if w in said:
                    return "refuse", 0.0
            mute_ts = float(s.get("mute_until") or 0)
            if mute_ts > now:
                return "refuse", 0.0  # 显式抑制期同样不开窗
        promise = s.get("promise") or {}
        has_promise = bool(promise.get("word")) and 0 <= float(promise.get("event") or -1) <= float(a.get("win_at") or 0)
        bye = any(w in str(a.get("text") or "") for w in self.c_bye_words)
        tw = self._t_warm_get(umo)
        T = float(tw.get("t") or 0.5) if float(tw.get("ts") or 0) > time.time() - 6 * 3600 else self._warm_t(umo)
        if has_promise:
            f = random.uniform(*INTENT_POOL_PROMISE)
            return "promise", round(f, 3)
        c = min(0.35 + 0.5 * T, 0.7) if bye else 0.55 + 0.3 * T
        if bye:
            c = min(c, 0.7)
        lo, hi = max(0.05, c - 0.1), min(0.95, c + 0.1)
        return ("bye" if bye else "plain"), round(random.uniform(lo, hi), 3)

    @staticmethod
    def _lognorm_hazard_p(el_min: float) -> float:
        """对数正态 hazard 形状因子（0~1）：峰在 PEAK_MIN、σ=SIGMA。
        先升（该回来了怎么还没冒头）后降（再不来心就凉）——直接用密度比 φ(z)/φ(z_peak)，
        危险率的"先升后降"形状由密度主导（峰后 φ 衰减快于 1/t），数值稳定"""
        if el_min <= 0.5:
            return 0.0
        mu = math.log(AFK_CHECK_PEAK_MIN)  # 密度峰位 ln(t*)=mu（hazard 峰略右移，形状足够）
        z = (math.log(el_min) - mu) / AFK_CHECK_SIGMA
        zm = 0.0
        pdf = math.exp(-0.5 * z * z)
        pdfm = math.exp(-0.5 * zm * zm)
        return max(0.0, min(1.0, pdf / pdfm))

    async def _afk_check_eval(self, umo: str, now: float, phase: str):
        """回归窗内逐 tick 掷（挂在 _silence_eval 之后调用）"""
        if not self.c_afk_check_on:
            return
        try:
            if phase != "awake" or not self._in_quiet(now):
                return
            s = self._sess(umo)
            a = self._afk_check_anchor(umo, now)
            if not a:
                return
            # 互斥：本事件已 miss 发过 → 跳过；dice done 的事件也跳
            d = s.get("dice") or {}
            if d.get("done") or (d.get("rolled") and float(d.get("event") or 0) == float(s.get("act_ts") or -1)):
                a["done"] = True
                self._save_state()
                return
            if a.get("f_intent") is None:
                intent, f = self._afk_intent(umo, a)
                if intent == "refuse":
                    a["done"] = True
                    a["f_intent"] = 0.0
                    a["intent"] = "refuse"
                    self._dlog({"t": "afk_check_void", "why": "refuse"})
                    self._save_state()
                    return
                a["intent"], a["f_intent"] = intent, f
                self._save_state()
            intent, f = a.get("intent"), float(a.get("f_intent") or 0)
            T = float(self._t_warm_get(umo).get("t") or 0.5)
            P = self._P(umo)
            drift = self._drift_vals(umo)
            base = float(drift.get("base", P.base_p))
            f_reply = self._rw_factor(umo)
            heat_r = int(a.get("rounds") or 0)
            f_heat = self._heat_factor(heat_r, umo)
            mood = float(self._st.get("mood", 1.0))
            p_cum = min(0.80, max(0.15, base * f_reply * f_heat * mood * f))
            # 逐 tick：P 按形状因子摊（90min/30s=180 tick；形状峰≈1 时 tick p≈P/90）
            el_min = (now - float(a.get("win_at") or now)) / 60.0
            shape = self._lognorm_hazard_p(el_min)
            p_tick = p_cum * shape / 90.0 * 1.5  # 峰值 tick 附近合计≈P（形状积分≈60）
            if random.random() >= p_tick:
                return
            peak_ts = datetime.fromtimestamp(float(a.get("win_at")) + AFK_CHECK_PEAK_MIN * 60).strftime("%H:%M")
            self._dlog({"t": "afk_check_roll", "word": a.get("word"), "p_cum": round(p_cum, 3),
                        "peak_ts": peak_ts, "intent": intent, "f_intent": f, "warm": T})
            a["done"] = True
            s["dice"] = {"event": float(s.get("act_ts") or 0), "rolled": True,
                         "done": True, "n": 1, "max_n": 1}
            self._save_state()
            # 发送走既有 fire 通道（模板生成），不发 repeated 追问
            self._st.setdefault("_afk_fire", {})[umo] = {"word": a.get("word"), "intent": intent}
            import asyncio
            asyncio.ensure_future(self._afk_check_fire(umo))
        except Exception:
            logger.warning("[主动私聊] 回归问候评估异常（跳过本轮）", exc_info=True)

    async def _afk_check_fire(self, umo: str):
        try:
            info = (self._st.get("_afk_fire") or {}).pop(umo, None)
            if not info:
                return
            now = time.time()
            if not self._quota_ok(now, umo):
                return
            tpl = self.tpl_afk_check.replace("{afk_word}", str(info.get("word") or ""))
            if await self._fire("afk_check", tpl, only_qq=str(umo.split(":")[-1])):
                self._st["random_sent"] = int(self._st.get("random_sent", 0)) + 1
                self._dlog({"t": "afk_check_sent", "word": info.get("word"),
                            "intent": info.get("intent")})
        except Exception:
            logger.warning("[主动私聊] 回归问候发送异常", exc_info=True)

    # ---------- v2.3.0 重逢委屈 / 被晾小委屈（表达层：一次性事实注入，不碰概率） ----------

    def _reunion_block(self, umo: str, now: float) -> str:
        """重逢委屈事实块：他特别久没来突然回来。时机=人类时间常数（7~10天封顶），
        强度随 T_before；无历史 fail-open 返回空"""
        try:
            if not self.c_reunion_on:
                return ""
            s = self._sess(umo)
            ts = [float(x) for x in (s.get("msg_times") or []) if float(x) > 0]
            if len(ts) < 2:
                return ""  # 无历史不委屈
            gap = float(s.get("_reunion_gap") or 0) or (now - ts[-1])
            # E_eff：消息间隔中位（跟自己基线比，钳 6h~36h）
            ivs = sorted(b - a for a, b in zip(ts, ts[1:]) if 0 < b - a < 14 * 86400)
            if not ivs:
                return ""
            e_med = ivs[len(ivs) // 2]
            e_eff = min(REUNION_E_HI, max(REUNION_E_LO, e_med))
            tier_i = 0
            for i, (k, lo, hi) in enumerate(REUNION_TIERS):
                if gap >= min(k * e_eff, lo):
                    tier_i = i + 1
            if not tier_i:
                return ""
            tier_names = ("轻哼一声", "明显委屈", "大委屈/闹别扭")
            # 被晾证据：期间她的主动（dlog miss/night/morning sent）没回
            ignored = int(s.get("unans") or 0)
            since = datetime.fromtimestamp(ts[-1]).strftime("%m-%d %H:%M")
            ratio = round(gap / e_eff, 1)
            days = round(gap / 86400, 1)
            t_before = float((s.get("t_warm_before") or {}).get("t") or 0.5)
            if t_before < 0.35:
                flavor = "你们最近本来就有点淡，你不必闹，淡淡的就好"
            elif tier_i == 3:
                flavor = "你攒了很久的委屈，这下全有了由头"
            else:
                flavor = "你有点小情绪"
            lines = [f"距TA上一条消息（{since}）已 {days} 天——你们平常平均 {round(e_eff/3600,1)} 小时一聊，这次隔了 {ratio} 倍"]
            if ignored > 0:
                lines.append(f"这期间你主动找过TA {ignored} 次，TA都没回")
            lines.append(f"TA现在回来了。此刻你的状态：{tier_names[tier_i-1]}（{flavor}）。反应按你们的关系和你的性格来，别提系统")
            self._dlog({"t": "reunion", "gap_days": days, "ratio": ratio,
                        "tier": tier_i, "t_before": t_before, "ignored_n": ignored})
            return "[重逢事实：" + "；".join(lines) + "]\n"
        except Exception:
            return ""

    def _sulk_block(self, umo: str, now: float) -> str:
        """被晾小委屈事实块：她找了他、他晾着她（小时级）。他开口时一次性注入"""
        try:
            if not self.c_sulk_on:
                return ""
            s = self._sess(umo)
            sk = s.get("sulk") or {}
            if not sk.get("sent_at"):
                return ""
            if now - float(sk.get("sent_at") or 0) > SULK_VALID_H * 3600:
                s["sulk"] = None
                s["sulk_due"] = None
                return ""
            wait_h = round((now - float(sk.get("sent_at") or 0)) / 3600, 1)
            promise = "，你明明说过「%s」" % sk["promise_word"] if sk.get("promise_word") else ""
            sent_at = datetime.fromtimestamp(float(sk.get("sent_at") or 0)).strftime("%H:%M")
            self._dlog({"t": "sulk", "wait_h": wait_h, "promise": bool(sk.get("promise_word")),
                        "thr_h": sk.get("thr_h")})
            s["sulk"] = None  # 一次性
            return ("[小委屈事实：你 %s 主动找过TA%s，TA隔了 %s 小时到现在才回——"
                    "你有点被晾着的小情绪，哼一句就行，反应按你们的相处来，别提系统]\n"
                    % (sent_at, promise, wait_h))
        except Exception:
            return ""

    def _sulk_arm(self, umo: str, sent_at: float):
        """她的主动发出后布防小委屈（阈值按热度投掷；夜间+8h）。
        热度=发送前那场对话的轮数（act_ts 是她发出前的最后事件）"""
        try:
            if not self.c_sulk_on:
                return
            s = self._sess(umo)
            last = float(s.get("act_ts") or sent_at)
            rounds = self._heat_rounds(umo, last)
            hot_n = min(1.0, rounds / max(1, self.c_heat_hot_min))
            thr_h = (SULK_THR_BASE_H - SULK_THR_SPAN_H * hot_n) * random.uniform(0.75, 1.25)
            thr_h = min(SULK_THR_CLAMP[1], max(SULK_THR_CLAMP[0], thr_h))
            dt = datetime.fromtimestamp(sent_at)
            if dt.hour >= 22 or dt.hour < 9:
                thr_h += SULK_NIGHT_ADD_H
            pr = s.get("pending_reply") or {}
            s["sulk"] = {"sent_at": sent_at, "thr_h": round(thr_h, 2),
                         "promise_word": (s.get("promise") or {}).get("word") or "",
                         "rounds": rounds}
            s["sulk_due"] = sent_at + thr_h * 3600
        except Exception:
            pass

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

    # ---------- v2.6.0 日切 ----------

    async def _day_cut_eval(self, now: float, phase: str):
        """醒后窗口内做当日日切（每目标一次）：种子成功才切（原子性）；失败按 10 分钟重试至
        2 小时，全失败回落旧行为（当天不切，记忆无损，账单=旧水平）；昨天没聊够不折腾"""
        if phase != "awake":
            return  # 临时清醒不切——只认真正醒了
        sl = self._sleep_state() or {}
        wake_ts = float(sl.get("wake_at") or 0)
        if not wake_ts or now - wake_ts > DAY_CUT_WAKE_WINDOW:
            return  # 没醒过/醒太久（重启错过窗口）=当天不切
        wday = datetime.fromtimestamp(wake_ts).date().isoformat()
        cuts = self._st.setdefault("day_cut", {})
        for umo in self._target_umos():
            st = cuts.get(umo) or {}
            if st.get("date") == wday and (st.get("cut") or st.get("gave_up")):
                continue  # 今天已切/已放弃
            if st.get("date") != wday:
                st = {"date": wday, "cut": False, "gave_up": False, "tries": 0, "next_at": 0.0}
            if now < float(st.get("next_at") or 0):
                continue
            st["tries"] = int(st.get("tries") or 0) + 1
            try:
                ok, noop = await self._day_cut_one(umo)
            except Exception:
                logger.warning("[主动私聊] 日切异常（今日回落旧行为，记忆无损）", exc_info=True)
                ok, noop = False, False
            if ok:
                st["cut"] = True
                self._dlog({"t": "day_cut", "umo": umo.split(":")[-1], "noop": noop,
                            "tries": st["tries"]})
            elif noop:
                st["cut"] = True  # 昨天没聊够：销账不折腾
            elif st["tries"] >= DAY_CUT_MAX_TRIES:
                st["gave_up"] = True
                self._dlog({"t": "day_cut_giveup", "umo": umo.split(":")[-1],
                            "why": "种子重试2小时全失败,当日回落旧行为"})
            else:
                st["next_at"] = now + DAY_CUT_RETRY_MIN * 60
            cuts[umo] = st
            self._save_state()

    async def _day_cut_one(self, umo: str) -> tuple:
        """对单个目标执行日切。返回 (成功?, 无需切?)。原子性：种子生成成功才建新对话——
        绝不存在'新对话空了、旧对话又丢上下文'的中间态。旧对话留在库里（可回滚）"""
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return True, True  # 从没聊过：无对话可切
        conv = await cm.get_conversation(umo, cid)
        history = getattr(conv, "history", None)
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list) or len(history) < DAY_CUT_MIN_HIST:
            return True, True  # 昨天没聊几条：上下文本来就小，不折腾
        summary = await self._gen_seed(umo, history)
        if not summary:
            return False, False  # 种子失败：不切（调用方安排重试）
        seed_msg = {"role": "user", "content":
                    "[系统标记：你睡醒后对昨天的记忆——自然记得的梗概，不是TA刚发的消息]\n" + summary}
        await cm.new_conversation(
            umo, content=[seed_msg],
            persona_id=getattr(conv, "persona_id", None))
        return True, False

    async def _gen_seed(self, umo: str, history: list) -> str:
        """昨日摘要：优先复用主对话真实系统提示词+原生 history 走 provider——前缀与主对话
        同字节，50K 历史吃缓存价（种子从全价4毛降到分位）；没捕获过系统头则裸调（全价兜底）"""
        try:
            try:
                provider = await self.context.get_using_provider_async(None)
            except Exception:
                provider = self.context.get_using_provider()
            if provider is None:
                return ""
            instr = self.tpl_seed
            sys_cap = (getattr(self, "_cap_sys", None) or {}).get(umo) or ""
            if sys_cap:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=instr, session_id=f"{PLUGIN_NAME}_seed",
                                       contexts=history, system_prompt=sys_cap),
                    timeout=180)
            else:
                lines = []
                for m in history:
                    if isinstance(m, dict):
                        who = "她" if m.get("role") == "assistant" else "TA"
                        lines.append(f"{who}: {self._record_text(m.get('content', ''))[:200]}")
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=instr + "\n\n聊天记录：\n" + "\n".join(lines),
                                       session_id=f"{PLUGIN_NAME}_seed"),
                    timeout=180)
            return str(getattr(resp, "completion_text", "") or "").strip()[:1500]
        except Exception:
            logger.warning("[主动私聊] 昨日种子生成失败（将重试，未切对话）", exc_info=True)
            return ""

    def _morning_reply_tpl(self, umo: str) -> str:
        """v2.5.2 留言回应模板按人分流：QQ 映射 > 配置映射(前缀/default) > 全局默认模板"""
        if self._mr_map:
            qq = str(umo.split(":")[-1])
            txt = self._mr_map.get("qq:" + qq) or ""
            if not txt:
                cid = self._conf_of(umo)
                if cid:
                    txt = self._mr_map.get("conf:" + cid.lower()) or ""
                    if not txt:
                        for k, v in self._mr_map.items():
                            if k.startswith("conf:") and cid.lower().startswith(k[5:]):
                                txt = v
                                break
            if not txt:
                txt = self._mr_map.get("conf:default") or ""
            if txt:
                return txt
        return self.tpl_morning_reply

    @staticmethod
    def _cfg_write_raw(cfg: dict, key: str, value) -> bool:
        """把键写进配置 dict 的所在分组（找不到落顶层）——供预设就地改写用"""
        if not isinstance(cfg, dict):
            return False
        for k, v in cfg.items():
            if isinstance(v, dict) and key in v:
                v[key] = value
                return True
        cfg[key] = value
        return True

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
            # v2.6.2 B5 翻转：只有@她的群聊(seen_chat)才算"陪聊中不算分别"软重置；
            # 跟别人聊(seen)不重置——人就在那儿却不找她，钟照走（掷中侧另有×1.4加成+醋口吻）
            # v2.6.3 开关：用户可裁定"群里@她算不算理她"——关=群聊不算私聊陪伴，钟照走
            seen_chat = float((self._st.get("seen_chat") or {}).get(qq) or 0)
            if self.c_group_at_counts and seen_chat > ev:
                ev = seen_chat
            # v2.7.0 约过期清理：锚过 2h 还没等到人、她今天也没主动发过 → 销约+缺席留痕
            # （抗规律感①：次日感官包自然带出"约了没等到"，她开口轻轻提一嘴——
            #  他当晚回来会在 _stamp 里把留痕作废掉）
            pl = s.get("promise") or {}
            if pl.get("word") and now > self._anchor_of(pl) + PROMISE_EXPIRE_H * 3600:
                fired_today = datetime.now().date().isoformat() in [f for f in (s.get("fires") or []) if f]
                if not fired_today:
                    self._st.setdefault("miss_trace", {})[umo] = {"word": pl["word"], "ts": now, "said": False}
                    self._dlog({"t": "promise_trace", "umo": qq, "word": pl["word"]})
                s["promise"] = None
            # v2.7.4 过期离席锚退役：超过该词预期×2.5（封顶8h）还没翻篇 → 从状态里删掉
            #（感官包注入侧同口径三段，见 _status_ctx——治"前天的吃饭喂到今天"）
            _a_gc = s.get("afk") or {}
            if _a_gc.get("word"):
                _a_d = self._afk_dur_learn_min(_a_gc["word"])
                if (now - float(_a_gc.get("event") or 0)) > min(_a_d * 2.5, 480) * 60:
                    s["afk"] = None
                    self._dlog({"t": "afk_expire", "umo": qq, "word": _a_gc["word"]})
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
            # v2.7.0 锚前不掷：约在身且没到点 → 静默钟压零（hazard=0 本 tick 必不掷）——
            # 她知道晚上有约，下午不瞎找；骰子从锚点起爬
            if tier == "promise":
                _pa = self._anchor_of(s.get("promise") or {})
                if now < _pa:
                    silence = 0.0
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
            # v2.6.2 群冷落加成：他刚在群里跟别人聊过（10分钟内，非@她）——人就在屏幕前，
            # 发消息零打扰所以敢发；"跟别人聊却不找她"的酸味在发送侧口吻里
            seen = float((self._st.get("seen") or {}).get(qq) or 0)
            if 0 < now - seen < SEEN_WHISPER_WIN:
                p = min(1.0, p * 1.4)
                factors["grp_seen"] = 1.4
            if attempt == 1:
                # 本事件总次数随热度抽（聊得越热她想起你的次数越多；抽的，观察不出固定数）
                if rounds >= self.c_heat_hot_min:
                    max_n = random.randint(2, 3)
                elif rounds <= self.c_heat_cold_max:
                    max_n = 1
                else:
                    max_n = random.randint(1, 2)
                # v2.6.4 有约档加一掷（用户 2026-11-05 拍板）：他留了"晚点找你"的约，单掷封盘
                # 会让82%的高概率一次输光后数小时无票（实测 10:30 输后 4h 干等）——有约在身多想一次
                if tier == "promise":
                    max_n += 1
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
            # v2.7.0 重掷间隔：性格区间 × 热度(热0.45/中0.7/冷1) × 有约0.4 × 空闲日0.6，
            # 下限 45min，右截今晚入睡-30min，不落频控禁发段；今晚放不下 → 本轮作废
            # 不硬塞（次日晨间线接手）——详见 _next_retry_at
            nxt = 0.0
            if attempt < max_n:
                nxt = self._next_retry_at(umo, now, tier, rounds, P)
                if not nxt:
                    self._dlog({"t": "retry_void", "umo": qq, "why": "今晚放不下",
                                "tier": tier, "n": attempt})
            s["dice"] = {"event": ev, "rolled": True, "n": attempt, "max_n": max_n,
                         "t0": t0, "tau": round(tau_eff, 3),
                         "haz": round(p_haz, 4), "p": round(p, 3),
                         "factors": factors, "tier": tier,
                         "next_at": nxt,
                         **({"done": True, "result": "no_room"} if not nxt and attempt < max_n else {})}
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
        # v2.6.1 修 B5 交互bug：群在场软重置把 pending 锚推到 seen_ts(>act_ts)后，
        # 旧的 != 判定永远不相等→掷中必被误杀"你回来过"。正确语义=只有他真的在私聊
        # 又说过话（act_ts 比 pending 锚更新）才算翻篇；seen 推前的锚不算
        if float(s.get("act_ts") or -1) > float(s.get("pending", {}).get("event") or 0):
            return False, "事件翻篇(你回来过)"
        # v2.7.0 群@联动（跟 dm_group_at_counts 走）：中签后他在群里@过她 = 见过面翻篇
        if self.c_group_at_counts:
            sc = float((self._st.get("seen_chat") or {}).get(str(umo.split(":")[-1])) or 0)
            if sc > float(s.get("pending", {}).get("event") or 0):
                return False, "事件翻篇(群里@过)"
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

    def _promise_fire_note(self) -> str:
        """v2.7.0 由头掷骰（抗规律感④）→ 约档生成附注：直赴约/借由头/带情绪 5:3:2。
        约是隐藏动机，说出来的理由另抽——来的姿态每次都不一样"""
        r = random.random()
        if 0.5 <= r < 0.8:
            return "（这条别直接提约定本身——像刚好看到个什么东西顺口发给他，由头是幌子，其实是等到点想他了）\n"
        if r >= 0.8:
            return "（等了这会儿还没等到人，这条带点小情绪：想念浓一点、或一点点小委屈、或困得没什么力气，挑一种，别真生气）\n"
        return ""

    def _next_retry_at(self, umo: str, now: float, tier: str, rounds: int, P) -> float:
        """v2.7.0 重掷到点时刻（0=今晚作废不硬塞）：miss_retry_gap 性格区间 × 乘子
        体系（热度×有约×空闲日），下限 45min，右截今晚评估窗尾（auto 窗=今晚入睡-30min），
        最早也得等频控 min_gap 过去——中签到点被频控拦下作废的票，从排期就避开"""
        m = (RETRY_MULT_HEAT[0] if rounds >= self.c_heat_hot_min
             else RETRY_MULT_HEAT[2] if rounds <= self.c_heat_cold_max else RETRY_MULT_HEAT[1])
        if tier == "promise":
            m *= RETRY_MULT_PROMISE
        if self._is_free_day():
            m *= RETRY_MULT_FREE
        lo = max(RETRY_FLOOR_MIN / 60.0, P.retry_gap_lo * m)
        hi = max(lo, P.retry_gap_hi * m)
        _, qhi = self._quiet_window()
        nd = datetime.fromtimestamp(now)
        remain_h = max(0.0, (qhi - (nd.hour * 60 + nd.minute)) / 60.0)
        if lo >= remain_h:
            return 0.0
        at = now + random.uniform(lo, min(hi, remain_h)) * 3600
        at = max(at, float(self._st.get("last_dm_ts") or 0) + self.c_gap * 3600 + 300)
        if at - now > remain_h * 3600:
            return 0.0
        return at

    def _grp_meet_clear(self, qq: str, pid: str):
        """v2.7.0 群里@她=见面（跟 dm_group_at_counts 开关走，三行为绑一开关零新键）：
        销约+销离席（见过面了不算"还没兑现"）+ 已中签 pending 作废（见过=翻篇）。
        想念钟软重置由 _silence_eval 的 seen_chat 折叠完成，这里不重复"""
        umo = f"{pid}:FriendMessage:{qq}"
        fs = self._sess(umo)
        cleared = []
        if fs.get("promise"):
            fs["promise"] = None
            cleared.append("约")
        if fs.get("afk"):
            fs["afk"] = None
            cleared.append("离席")
        if fs.get("pending"):
            fs["pending"] = None
            self._dice_done(umo)
            cleared.append("中签")
        if cleared:
            self._dlog({"t": "grp_meet", "umo": qq, "cleared": "/".join(cleared)})
            self._save_state()

    async def _fire_miss(self, umo: str, now: float):
        """中签发送：否决（可选）→ 情景门控工具生成 → 拟人发送 → 记账"""
        s = self._sess(umo)
        if not self._send_open(umo, now):
            return  # D7 发送熔断中：本轮不掷发（pending 保留，退避后再看）
        rounds = self._heat_rounds(umo, now)
        tier = (s.get("dice") or {}).get("tier") or "normal"
        tag = self._scenario_tag(umo, tier)
        # v2.5.0 B6 迟到晨间口吻：晨起骰输且这是她今天对这个号的首条主动 → 开口带刚醒味
        today = datetime.now().date().isoformat()
        mplan = (self._st.get("morning") or {}).get(umo) or {}
        first_today = self._per_cnt(umo, "sent") == 0
        if mplan.get("key") == today and mplan.get("win") is False and first_today:
            tag = "睡醒后还没说过话"
        extra_ctx = ""
        if tag == "睡醒后还没说过话":
            extra_ctx = "（你睡醒到现在还没主动跟TA说过话，这条开口自然带点刚醒的感觉，不用硬找话题）\n"
        # v2.6.2 B5 翻转后的口吻：他刚在群里跟别人聊得欢（非@她）→ 醋味冷落档——
        # "人就在那儿跟别人聊，却不来找我"是冷落信号不是宽慰，戳一句带酸
        qq = str(umo.split(":")[-1])
        seen_chat = float((self._st.get("seen_chat") or {}).get(qq) or 0)
        if (not self.c_group_at_counts) and seen_chat and 0 < now - seen_chat < SEEN_WHISPER_WIN:
            _priv_gap = max(0.0, (now - float(s.get("act_ts") or now)) / 3600)
            extra_ctx += (f"（你们刚在群里聊过，但私聊已经 {_priv_gap:.0f} 小时没说话了——群里的热闹不算数，"
                          "你想要他来私聊找你，这条可以带点这个意思，比如群里太吵、有话想单独跟他说，"
                          "不委屈不吵架，就是想要私聊）\n")
        seen = float((self._st.get("seen") or {}).get(qq) or 0)
        if seen and 0 < now - seen < SEEN_WHISPER_WIN:
            _priv_gap = max(0.0, (now - float(s.get("act_ts") or now)) / 3600)
            extra_ctx += (f"（TA这会儿正在群里跟别人聊得欢，你们私聊已经 {_priv_gap:.0f} 小时没说话了——"
                          "你这条是私聊发的，像凑过去小声说一句，可以带点酸：群里聊那么欢都不来找我，"
                          "但别真吵架，傲娇的小委屈那种）\n")
        # v2.7.0 由头掷骰（抗规律感④）：约档赴约不总走正门——直赴约/借由头/带情绪 5:3:2
        if tier == "promise":
            extra_ctx += self._promise_fire_note()
        if self.c_gen_mode.get("random") == "template":
            text = random.choice(self.c_pool)
            status = "ok"
        else:
            tier_note = ("" if tag == "睡醒后还没说过话" else
                         "去吃饭离席" if tier == "afk" else "约定未赴" if tier == "promise"
                         else "聊嗨断线" if tier == "hot" else "普通冷场")
            scen_line = f"[本次情景：{tag}{'（' + tier_note + '）' if tier_note else ''}]\n"
            text = await self._gen(
                self.tpl_miss_random, umo=umo, kind="random",
                veto_ok=self.c_ai_veto,
                veto_ctx=self._status_ctx(umo) + self._together_ctx(umo, now)
                    + self._stance_line(umo)
                    + scen_line
                    + extra_ctx,
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
        # v2.7.0 缺席留痕销账：她已经主动开过口了，这一页翻过去（晨间不再重复提）
        tr = (self._st.get("miss_trace") or {}).get(umo)
        if tr and not tr.get("said"):
            tr["said"] = True
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
        """补发指令文件：外部/管理往 data/makeup.json 写 {"kind":"night"} 即触发，读后即删。
        v2.5.0 A4：可加 "qq" 字段定向补发（不填=全员广播）"""
        try:
            p = self._data_dir / "makeup.json"
            if p.exists():
                v = json.loads(p.read_text(encoding="utf-8"))
                p.unlink(missing_ok=True)
                if isinstance(v, dict) and v.get("kind"):
                    return {"kind": str(v["kind"]), "qq": str(v.get("qq") or "").strip() or None}
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

    def _send_open(self, umo: str, now: float = None) -> bool:
        """v2.5.0 D7 发送熔断闸门：该会话是否允许发送（连续失败指数退避中=关）"""
        now = now or time.time()
        return now >= float(self._sess(umo).get("send_break_until") or 0)

    async def _send_human(self, umo: str, text: str) -> None:
        """拟人发送：按句切分逐条发；下一条的间隔=按它的长度算的打字时间
        （每字 0.10~0.18 秒随机，下限 0.8s、上限 8s）——长句等得久，像真人输入，也防风控。
        v2.5.0 D7：失败记连败（≥3 指数退避），成功清零"""
        segs = self._split_text(text) if self.c_seg_on else [text]
        try:
            for i, seg in enumerate(segs):
                if i:
                    typing = min(8.0, max(0.8, len(segs[i]) * random.uniform(0.10, 0.18)))
                    await asyncio.sleep(typing)
                await self.context.send_message(umo, MessageChain().message(seg))
        except Exception:
            s = self._sess(umo)
            fail = int(s.get("send_fail") or 0) + 1
            s["send_fail"] = fail
            if fail >= SEND_FAIL_N:
                break_min = min(SEND_BREAK_CAP_MIN, SEND_BREAK_BASE_MIN * (2 ** (fail - SEND_FAIL_N)))
                s["send_break_until"] = time.time() + break_min * 60
                self._dlog({"t": "send_break", "umo": umo.split(":")[-1], "fail": fail,
                            "break_min": break_min})
            raise
        s = self._sess(umo)
        s["send_fail"] = 0
        s["send_break_until"] = 0.0

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
                if not self._send_open(umo, now):
                    continue  # D7 发送熔断中：跳过该目标（不炸重试）
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
                _vctx = None
                if kind == "afk_check":
                    # v2.3.0 回归问候：生成带上背景事实（TA几点说词/隔了多久/约定原词）
                    # 与第五类情景标签——不然模板干念、她不知道"按惯例该回来了"是什么
                    _s = self._sess(umo)
                    _pl = _s.get("promise") or {}
                    _hasp = bool(_pl.get("word")) and float(_pl.get("event") or 0) > 0
                    _tag = "离席到点未归·有约在身" if _hasp else "离席到点未归"
                    _w = (_s.get("afk_check") or {}).get("word") or "离开"
                    _vctx = (self._status_ctx(umo) + self._stance_line(umo)
                             + "[本次情景：" + _tag + "（TA说" + _w + "离开，到该回来的点还没冒头）]" + chr(10))
                text = await self._gen(use_tpl, umo, kind=kind, veto_ctx=_vctx)  # 按目标逐个生成（上下文/工具各不同）
                try:
                    await self._send_human(umo, text)
                    sent_any = True
                    logger.info(f"[主动私聊] {kind} -> {qq}: {text[:40]}")
                    # R7：带工具的轮次把工具轨迹拼进归档——否则后续上下文里
                    # "她不知道自己刚发过那张图/那首歌"，容易穿帮。
                    # 注记从紧：至多3条、每条结果截25字（归档无硬上限，省的是上下文token）
                    self._sulk_arm(umo, now)
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
        （用户先开口=晨间冲刷已发生，她已在对话里报过到）。
        v2.7.7：改用自己记的 msg_times 流水（per-umo、26h 落盘，不怕日切/重启）——
        原实现去对话历史消息文本里解析内嵌时间戳，那是群聊存档格式、私聊历史没有，
        检查从未生效过（11-07 实测：他 09:32 先开口，09:40 她又补"醒啦"重复报到）"""
        s = self._sess(umo)
        return any(t > float(ts) for t in (s.get("msg_times") or []))

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

    def _faces_hint(self) -> str:
        """v2.2.0：主动消息带上 faces 的风格注入。回复管线由 faces 挂 on_llm_request 自己注，
        dm 的生成不走那条钩子——这里主动取一次。faces 没装/没开/取不到一律返回空串，零影响。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_dafeiyu_faces")
            inst = getattr(meta, "star_cls", None) if meta is not None else None
            hint = getattr(inst, "_style_hint", None)
            if callable(hint):
                text = str(hint() or "").strip()
                if text:
                    return text + "\n\n"
        except Exception:
            pass
        return ""

    async def _memory_recall(self, umo: str, ctx: str) -> str:
        """v2.2.0：主动消息前预取长期记忆（livingmemory）。按目标会话显式隔离检索
        （各号各翻各的账本），拿最近聊天尾部当查询词；没装/查空/超时一律空串，零影响。"""
        if not (self.c_memory_on and umo):
            return ""
        query = ctx.strip()[-200:]
        if len(query) < 10:
            return ""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_livingmemory")
            inst = getattr(meta, "star_cls", None) if meta is not None else None
            eng = getattr(getattr(inst, "initializer", None), "memory_engine", None)
            search = getattr(eng, "search_memories", None)
            if not callable(search):
                return ""
            results = await asyncio.wait_for(
                search(query, k=self.c_memory_k, session_id=umo), timeout=8)
            lines = []
            for r in results or []:
                t = str(getattr(r, "content", "") or "").strip().replace("\n", " ")
                if t:
                    lines.append("- " + t[:120])
            if not lines:
                return ""
            return ("[你刚翻了下自己的旧记忆，这几条与现在最相关（参考用，别原文复述）]\n"
                    + "\n".join(lines) + "\n\n")
        except Exception:
            return ""

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

    async def _inner_monologue(self, persona_text: str, ctx: str, who: str, veto_hint: str = "",
                               fh: str = "", now_line: str = "") -> str:
        """两段式第一段：无工具酝酿内心戏。产物只拼进第二段 prompt，
        不发送、不归档、不落盘（防记忆污染）。失败抛异常由调用方降级单段。
        v1.5：veto_hint 非空时内心戏顺带做"发/不发"判断（AI 否决层，只有否决权）。
        v2.5.0 F③：fh/now_line 与正文同源——前缀字节与第二段完全一致，
        第二段调用命中第一段的前缀缓存（省一次全额输入）"""
        try:
            provider = await self.context.get_using_provider_async(None)
        except Exception:
            provider = self.context.get_using_provider()
        prompt = (
            (persona_text[:DM_PERSONA_SLICE] + "\n\n" if persona_text else "")
            + fh
            + ctx
            + (now_line or f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n")
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
                    # v2.2.0：带目标 UMO 取人设——走 UMOP 路由拿到该号的人设（296=唯一的/507=肥鱼(博)），
                    # 旧写法 None 永远落主配置默认（老版大肥鱼）
                    persona = await self.context.persona_manager.get_default_persona_v3(umo or None)
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
                mem_line = await self._memory_recall(umo, ctx)  # v2.2.0：预取长期记忆（隔离按目标会话）
                if mem_line:
                    ctx = mem_line + ctx
                now_line = f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n"
                fh = self._faces_hint()  # v2.2.0：faces 风格注入（颜文字/拟声词规则），没装=空串
                prompt = (
                    (persona_text[:DM_PERSONA_SLICE] + "\n\n" if persona_text else "")
                    + fh
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
                            fh=fh, now_line=now_line,
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
                                    inner = await self._inner_monologue(persona_text, ctx, who,
                                                                         fh=fh, now_line=now_line)
                                except Exception:
                                    logger.info("[主动私聊] 内心戏生成失败，本轮单段直出")
                            if inner:
                                p2 = (
                                    (persona_text[:DM_PERSONA_SLICE] + "\n\n" if persona_text else "")
                                    + fh
                                    + ctx
                                    + now_line
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

    @filter.command("主动私聊保存")
    async def cmd_save_tier(self, event: AstrMessageEvent, name: str = ""):
        """把发送者当前生效的挡位参数快照存进自定义槽（第一个空位，最多3个）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        name = (name or "").strip()
        if not name:
            yield event.plain_result("用法：/主动私聊保存 槽名（把当前挡位参数存成自定义挡）")
            return
        if name in _TEMPERAMENTS:
            yield event.plain_result(f"「{name}」是固定挡位名，换个名字")
            return
        slots = [str(x) for x in (self._flatten(self._cfg_obj or {}).get("miss_custom_slots") or []) if str(x).strip()]
        if any(x.split(":", 1)[0].strip() == name for x in slots):
            yield event.plain_result(f"槽「{name}」已存在（要覆盖先在面板删掉那行）")
            return
        line = self._tier_slot_line(event.unified_msg_origin, name)
        if not line:
            yield event.plain_result("没取到当前挡位参数（会话无记录？先跟TA说句话再试）")
            return
        if len(slots) >= 3:
            yield event.plain_result("三个自定义槽都满了。手动放：" + chr(10) + line)
            return
        slots.append(line)
        self._cfg_write("miss_custom_slots", slots)
        try:
            await self._cfg_obj.save_config_async()
        except Exception:
            try:
                self._cfg_obj.save_config()
            except Exception:
                yield event.plain_result("槽行已生成但存盘失败，手动粘贴：" + chr(10) + line)
                return
        yield event.plain_result(f"已存为自定义挡「{name}」（槽{len(slots)}/3）。重载后即可在挡位里选用")

    @filter.command("主动私聊导出")
    async def cmd_export_tier(self, event: AstrMessageEvent):
        """回显当前挡位的槽行文本（面板复制粘贴用）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        line = self._tier_slot_line(event.unified_msg_origin)
        if not line:
            yield event.plain_result("没取到当前挡位参数")
            return
        yield event.plain_result("自定义槽行（复制到面板 miss_custom_slots）：" + chr(10) + line)

    def _tier_slot_line(self, umo: str, name: str = "") -> str:
        """发送者当前生效挡位 → 自定义槽格式一行（name=用户起的槽名，缺省用当前挡位名）"""
        try:
            P = self._P(umo)
            name = (name or "").strip() or self._tier_of(umo) or "我的挡"
            gap = "%g-%g" % (P.retry_gap_lo, P.retry_gap_hi)
            kv = [
                ("miss_base_p", P.base_p), ("miss_daily_max", P.daily_max),
                ("dm_daily_cap", P.cap),
                ("miss_rw_alpha", P.rw_alpha), ("miss_rw_r_fast", P.rw_r_fast),
                ("miss_rw_r_slow", P.rw_r_slow), ("miss_rw_r_none", P.rw_r_none),
                ("miss_rw_lo", P.rw_lo), ("miss_rw_hi", P.rw_hi),
                ("miss_cd_a", P.cd_a), ("miss_cd_tau", P.cd_tau),
                ("miss_haz_k", getattr(P, "haz_k", 2.0)),
                ("miss_haz_scale", getattr(P, "haz_scale", 1.0)),
                ("miss_floor", P.floor), ("miss_retry_tau", P.retry_tau),
                ("miss_retry_gap", gap),
                ("miss_heat_cold_factor", P.heat_cold_f), ("miss_heat_hot_factor", P.heat_hot_f),
                ("miss_heat_k", P.heat_k),
                ("miss_afk_min", P.afk_min), ("miss_mid_min", P.mid_min),
                ("miss_silence_min", P.silence_min),
                ("morning_base", P.morning_base), ("evening_base", P.evening_base),
            ]
            body = ", ".join("%s=%s" % (k, ("%g" % v) if isinstance(v, float) else v) for k, v in kv)
            return name + ": " + body
        except Exception:
            logger.warning("[主动私聊] 挡位导出失败", exc_info=True)
            return ""

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

    @staticmethod
    def _is_cmd_text(text: str) -> bool:
        """/ 开头=管理指令（不算聊天内容；唤醒前缀默认就是 /）"""
        return str(text).lstrip().startswith("/")

    @staticmethod
    def _qq_args(raw: str) -> set:
        """从指令原文里抠纯数字 token 当 QQ 号（"今晚骰子"本身不含数字，不会误中）"""
        return {t for t in str(raw).replace("@", " ").split() if t.isdigit() and len(t) >= 5}

    @filter.command("今晚骰子")
    async def cmd_dice_tonight(self, event: AstrMessageEvent):
        """v2.7.0 管理员自查：约的锚/掷骰进度/中签/留痕/心情一眼看全。
        v2.7.1 可带 QQ 号过滤（/今晚骰子 123456789）；任何你挂管理员的对话都能发
        （群里/临时会话都行，输出只回当前对话）——不用在跟她的私聊里发"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        self._rollover_day()
        want = self._qq_args(str(getattr(event, "message_str", "") or ""))
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"
        lines = [f"[今晚骰子 v{PLUGIN_VERSION}] 今日 {self._st.get('date')}"
                 f" 心情 {float(self._st.get('mood', 1.0)):.2f}"]
        tmap = self._target_map()
        matched = False
        for pid, qqs in tmap.items():
            for qq in qqs:
                if want and str(qq) not in want:
                    continue
                matched = True
                umo = f"{pid}:FriendMessage:{qq}"
                s = self._sess(umo)
                pl = s.get("promise") or {}
                if pl.get("word"):
                    pa = self._anchor_of(pl)
                    left = int((pa - time.time()) // 60)
                    p_line = (f"约「{pl['word']}」({pl.get('kind')}) 锚{fmt(pa)}"
                              + (f" 还剩{left}分" if left > 0 else f" 过了{-left}分")
                              + (f" 冒泡×{pl.get('bubble_n') or 0}" if pl.get("bubble_n") else ""))
                else:
                    p_line = "约无"
                d = s.get("dice") or {}
                if d.get("rolled"):
                    d_line = (f"骰{d.get('n')}/{d.get('max_n')} p={d.get('p', 0):.2f} 档[{d.get('tier')}]"
                              + (f" 下掷{fmt(d.get('next_at'))}" if float(d.get("next_at") or 0) > 0 else "")
                              + (f" 终({d.get('result') or '了'})" if d.get("done") else ""))
                else:
                    d_line = "骰未掷"
                pd = s.get("pending") or {}
                pd_line = (f" 中签{'顺延' if pd.get('deferred') else ''}→{fmt(pd.get('at'))}"
                           if pd.get("at") else "")
                _af = s.get("afk") or {}
                af_line = (f" 离席「{_af['word']}」{'已释放' if _af.get('released') else ''}"
                           if _af.get("word") else "")
                tr = (self._st.get("miss_trace") or {}).get(umo) or {}
                tr_line = (f" 留痕「{tr.get('word')}」{'已提' if tr.get('said') else '待提'}"
                           if tr.get("word") else "")
                lines.append(f"· {qq}：{p_line}｜{d_line}{pd_line}{af_line}{tr_line}")
        if want and not matched:
            _all = "/".join("/".join(qqs) for qqs in tmap.values()) or "无"
            lines.append(f"查无目标 {'/'.join(sorted(want))}（现有目标：{_all}）")
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
