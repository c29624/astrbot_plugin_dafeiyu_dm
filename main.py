"""大肥鱼·主动私聊插件

让 bot 在合适的时候主动找人说话（默认找 AstrBot 管理员）：
- 晨起：她醒来后 5~30 分钟，带着刚醒的劲儿来一句
- 睡前：今晚入睡点前几分钟，来说句晚安再睡
- 随机想念：白天清醒期，每天 N 次随机时刻"突然想起你"
- 作息联动：直接读睡觉插件（astrbot_plugin_dafeiyu_sleep）的状态文件——她睡着绝不主动发消息；
  没装睡觉插件也能独立运行（退化为纯时间窗模式）
- 内容：首选 LLM 现场生成（带现用人设），失败/超时回落语料池
- 频率保险：每日总量上限、两 次 最短间隔，风控友好
- /主动私聊状态 查看当日计划；/主动私聊测试 立刻给发送者来一条（均仅管理员）
"""

import asyncio
import json
import random
import re
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.session_llm_manager import SessionLLMManager
except ImportError:
    SessionLLMManager = None

PLUGIN_NAME = "astrbot_plugin_dafeiyu_dm"
PLUGIN_VERSION = "1.2.6"
SLEEP_STATE_CANDIDATES = [
    Path("data/plugin_data/astrbot_plugin_dafeiyu_sleep/sleep_state.json"),
]
TICK_SECONDS = 30
NIGHT_WINDOW_MIN = 10  # 入睡点前多少分钟内发睡前消息

DEFAULTS = {
    "dm_enabled": True,
    "dm_targets": [],          # 留空 = 自动用 AstrBot 管理员列表
    "dm_platform": "",         # 留空 = 自动发现第一个平台
    "dm_owner_alias": "对方",   # 模板变量 {who}：对方怎么称呼收信人（大肥鱼填"主人"）
    "dm_alias_map": [],           # 按 QQ 配称呼：["10001=主人"]，没配的用全局
    "dm_seg_enabled": True,      # 主动消息按句切分逐条发（拟人+防风控）
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
}


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


@register(
    PLUGIN_NAME,
    "29624",
    "主动私聊：晨起/睡前/随机想念，联动睡觉插件作息，她睡着不打扰",
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
        self.c_ctx_n = max(3, min(40, int(g("dm_context_count") or 10)))
        self.c_respect_sleep = bool(g("dm_respect_sleep"))
        self.c_morning = bool(g("dm_morning_on"))
        self.c_night = bool(g("dm_night_on"))
        self.c_random_n = max(0, min(5, int(g("dm_random_count") or 0)))
        self.c_win_lo = _parse_hhmm(g("dm_window_start")) or 10 * 60
        self.c_win_hi = _parse_hhmm(g("dm_window_end")) or 22 * 60
        self.c_gap = max(0.0, float(g("dm_min_gap_hours") or 0))
        self.c_cap = max(1, int(g("dm_daily_cap") or 1))
        self.tpl_morning = str(g("dm_morning_template"))
        self.tpl_night = str(g("dm_night_template"))
        self.tpl_random = str(g("dm_random_template"))
        self.tpl_makeup_night = str(
            g("dm_makeup_night_template")
        )
        self.tpl_night2 = str(g("dm_night2_template"))
        pool = [str(s).strip() for s in (g("dm_fallback_pool") or []) if str(s).strip()]
        self.c_pool = pool or list(DEFAULTS["dm_fallback_pool"])
        # 当日执行状态（dm_state.json 持久化，跨重启不重发不丢计划）
        self._st = {}
        self._data_dir = None
        self._task = None

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
        """跨天滚动：清计数、按窗口生成今天的随机想念计划"""
        today = datetime.now().date().isoformat()
        if self._st.get("date") == today:
            return
        now = datetime.now()
        lo_dt = now.replace(hour=self.c_win_lo // 60, minute=self.c_win_lo % 60, second=0, microsecond=0)
        hi_dt = now.replace(hour=self.c_win_hi // 60, minute=self.c_win_hi % 60, second=0, microsecond=0)
        plans = []
        if self.c_random_n and lo_dt < hi_dt:
            for _ in range(self.c_random_n):
                at = random.uniform(0, 1)
                ts = (lo_dt + (hi_dt - lo_dt) * at).timestamp()
                if ts > time.time():
                    plans.append(ts)
        self._st = {
            "date": today,
            "morning": None,   # {"key","at","sent"} 晨起计划
            "night": None,     # {"key","sent"} 睡前（按夜锚日去重）
            "random_plan": sorted(plans),
            "random_sent": 0,
            "sent_total": 0,
            "last_dm_ts": 0.0,
        }
        self._save_state()

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
        # 补发通道：data 目录出现 makeup.json（{"kind": "night"|"morning"|"random"}）即消费
        # ——绕过作息相位（管理员显式补发），仍走频控与总量；发完即删指令文件
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

        # 晨起：醒来后 5~30 分钟（计划时刻首次确定后持久化，不漂移）
        if self.c_morning:
            wake_ts = float(sl.get("wake_at") or 0)
            m = self._st.get("morning")
            if wake_ts and now - wake_ts < 6 * 3600:
                key = datetime.fromtimestamp(wake_ts).date().isoformat()
                if not m or m.get("key") != key:
                    m = {"key": key, "at": wake_ts + random.uniform(5, 30) * 60, "sent": False}
                    self._st["morning"] = m
                    self._save_state()
            if m and not m.get("sent") and now >= m.get("at", 0):
                if phase == "temp_wake":
                    m["at"] = now + 10 * 60  # 半梦半醒不是正式醒来，缓一缓
                    self._save_state()
                elif phase == "awake":
                    m["tries"] = int(m.get("tries", 0)) + 1
                    if await self._fire("morning", self.tpl_morning):
                        m["sent"] = True
                    elif m["tries"] >= 3:
                        m["sent"] = True  # 连续失败3次，销账防死循环
                        logger.warning("[主动私聊] 晨起问候重试3次未发出，放弃")
                    self._save_state()

        # 睡前：今晚入睡点前 NIGHT_WINDOW_MIN 分钟内、且她还清醒
        if self.c_night:
            win = sl.get("sleep_window") or []
            bed_ts = float(win[0]) if win else 0
            if bed_ts:
                key = datetime.fromtimestamp(bed_ts).date().isoformat()
                n = self._st.get("night")
                if not n or n.get("key") != key:
                    n = {"key": key, "sent": False}
                    self._st["night"] = n
                    self._save_state()
                if (
                    not n.get("sent")
                    and phase == "awake"
                    and 0 < bed_ts - now <= NIGHT_WINDOW_MIN * 60
                ):
                    n["tries"] = int(n.get("tries", 0)) + 1
                    if await self._fire("night", self.tpl_night):
                        n["sent"] = True
                    elif n["tries"] >= 3:
                        n["sent"] = True  # 连续失败3次，销账防死循环
                        logger.warning("[主动私聊] 睡前晚安重试3次未发出，放弃")
                    self._save_state()

        # 随机想念：到点即发；睡着则顺延半小时（窗口外作废）
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
                        rest.append(late)  # 睡着顺延，出窗作废
            self._st["random_plan"] = sorted(rest)
            self._save_state()

    def _quota_ok(self, now) -> bool:
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

    async def _fire(self, kind: str, template: str) -> bool:
        """返回是否真的发出去了——调用方据此记账，没发出不销账、窗口内自动重试"""
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
                umo = f"{pid}:FriendMessage:{qq}"
                use_tpl = template
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
                text = await self._gen(use_tpl, umo)  # 按目标逐个生成（上下文各不同）
                try:
                    await self._send_human(umo, text)
                    sent_any = True
                    logger.info(f"[主动私聊] {kind} -> {qq}: {text[:40]}")
                    await self._archive_dm(umo, text)
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
        时间从最后一条 user 记录内嵌的时间戳解析（chat_plus 存档格式
        "[YYYY-MM-DD 周X HH:MM:SS]"），解析不到就当没说过（不误伤正常发送）。"""
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
            for rec in reversed(history):
                if not (isinstance(rec, dict) and rec.get("role") == "user"):
                    continue
                txt = self._record_text(rec.get("content", ""))
                mm = re.search(r"(\d{4})-(\d{2})-(\d{2})[^\d]{1,4}(\d{2}):(\d{2}):(\d{2})", txt)
                if not mm:
                    return False
                spoke = datetime(
                    int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                    int(mm.group(4)), int(mm.group(5)), int(mm.group(6)),
                ).timestamp()
                return spoke > ts
            return False
        except Exception:
            return False

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

    async def _gen(self, template: str, umo: str = "") -> str:
        """首选 LLM 临场生成（现用人设 + 最近聊天上下文 + 当前时间）；失败/超时回落语料池"""
        who = self.c_alias
        try:
            qq = umo.split(":")[2] if umo else ""
            who = self.c_alias_map.get(qq, self.c_alias)
        except Exception:
            pass
        prompt_tpl = template.replace("{who}", who)
        try:
            try:
                provider = await self.context.get_using_provider_async(None)
            except Exception:
                provider = self.context.get_using_provider()
            if provider is not None:
                persona_text = ""
                try:
                    persona = await self.context.persona_manager.get_default_persona_v3(
                        None
                    )
                    if isinstance(persona, dict):
                        persona_text = str(persona.get("prompt") or persona.get("system_prompt") or "")
                    elif persona is not None:
                        persona_text = str(
                            getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "")
                        )
                except Exception:
                    persona_text = ""
                ctx = await self._recent_history(umo) if umo else ""
                now_line = f"[当前时间 {datetime.now().strftime('%H:%M')}]\n\n"
                prompt = (
                    (persona_text[:1500] + "\n\n" if persona_text else "")
                    + ctx
                    + now_line
                    + prompt_tpl
                )
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, session_id=f"{PLUGIN_NAME}_gen"),
                    timeout=25,
                )
                text = str(getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text[:120]
        except Exception:
            logger.warning("[主动私聊] LLM 生成失败，回落语料池", exc_info=True)
        return random.choice(self.c_pool)

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
        yield event.plain_result(
            f"[主动私聊] 今日 {self._st.get('date')}\n"
            f"目标：{'/'.join(self._targets()) or '（空）'} 平台：{self._platform_id() or '?'}\n"
            f"作息联动：{'读到了' if sl else '无睡觉数据（当白天跑）'} 当前相：{self._sleep_phase()}\n"
            f"晨起：{'已发' if m.get('sent') else (fmt(m.get('at')) if m else '无计划')}"
            f" 睡前：{'已发' if n.get('sent') else (n.get('key') or '无计划')}\n"
            f"随机想念：今日 {self._st.get('random_sent', 0)}/{self.c_random_n} 发，"
            f"剩余计划 {[fmt(t) for t in (self._st.get('random_plan') or [])]}\n"
            f"总量 {self._st.get('sent_total', 0)}/{self.c_cap}，"
            f"上次发送 {fmt(self._st.get('last_dm_ts')) if self._st.get('last_dm_ts') else '今天还没发过'}"
        )

    @filter.command("主动私聊测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """立刻给发送者本人私聊来一条（验证通道+人设）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        umo = str(event.unified_msg_origin)
        pid = umo.split(":")[0]  # 发送者所在平台
        sid = str(event.get_sender_id())
        text = await self._gen(self.tpl_random, umo=f"{pid}:FriendMessage:{sid}")
        try:
            await self._send_human(f"{pid}:FriendMessage:{sid}", text)
            yield event.plain_result(f"已私聊发送：{text[:60]}（测试不计入当日频控）")
        except Exception:
            yield event.plain_result("发送失败，看日志")
