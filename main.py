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
PLUGIN_VERSION = "1.0.2"
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
    "dm_respect_sleep": True,
    "dm_morning_on": True,
    "dm_night_on": True,
    "dm_random_count": 2,
    "dm_window_start": "10:00",
    "dm_window_end": "22:00",
    "dm_min_gap_hours": 2.0,
    "dm_daily_cap": 4,
    "dm_morning_template": (
        "你刚睡醒没多久，突然想跟{who}说句话。就一两句，带着刚醒的劲儿，"
        "自然一点，别提任何系统或作息数字。"
    ),
    "dm_night_template": (
        "你马上要去睡了，睡前想跟{who}说句晚安。就一句话，困困的，"
        "别提任何系统或作息数字。"
    ),
    "dm_random_template": (
        "你白天突然想起了{who}，想主动跟{who}说句话。一两句，"
        "像真人突然想起对方随手发条消息那样自然，可以从你正在做的事说起，别提系统。"
    ),
    "dm_fallback_pool": [
        "在吗？突然想到你了",
        "忙不忙？闲着说两句话",
        "想起个事，你那边怎么样",
        "冒个泡，证明我还活着",
        "没啥事，就是来看看你回没回消息",
    ],
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
        留空 → 按平台各自解析：多配置文件场景下，每个平台用它绑定配置文件的 admins_id
        （平台没绑/绑定只到具体会话 → 回落默认配置的管理员）。"""
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
            qs = [str(a).strip() for a in admins if str(a).strip()]
            if qs:
                result[pid] = qs
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
                    await self._fire("morning", self.tpl_morning)
                    m["sent"] = True
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
                    await self._fire("night", self.tpl_night)
                    n["sent"] = True
                    self._save_state()

        # 随机想念：到点即发；睡着则顺延半小时（窗口外作废）
        plans = self._st.get("random_plan") or []
        due = [t for t in plans if t <= now]
        if due:
            rest = [t for t in plans if t > now]
            for ts in due:
                if phase == "awake" and self._quota_ok(now):
                    await self._fire("random", self.tpl_random)
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

    # ---------- 发送 ----------

    async def _fire(self, kind: str, template: str):
        tmap = self._target_map()
        if not tmap:
            logger.info(f"[主动私聊] {kind}：无可用目标/平台，跳过")
            return
        now = time.time()
        if not self._quota_ok(now) and kind != "morning" and kind != "night":
            return  # 随机类受频控；晨起/睡前是作息事件，走自己的开关不受间隔限制（仍计总量）
        if int(self._st.get("sent_total", 0)) >= self.c_cap:
            return
        text = await self._gen(template)
        sent_any = False
        for pid, targets in tmap.items():
            for qq in targets:
                umo = f"{pid}:FriendMessage:{qq}"
                try:
                    await self.context.send_message(umo, MessageChain().message(text))
                    sent_any = True
                    logger.info(f"[主动私聊] {kind} -> {qq}: {text[:40]}")
                except Exception:
                    logger.warning(f"[主动私聊] {kind} 发送失败 {umo}", exc_info=True)
        if sent_any:
            self._st["sent_total"] = int(self._st.get("sent_total", 0)) + 1
            self._st["last_dm_ts"] = now
            self._save_state()

    async def _gen(self, template: str) -> str:
        """首选 LLM 临场生成（带现用人设）；失败/超时回落语料池"""
        prompt_tpl = template.replace("{who}", self.c_alias)
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
                prompt = (persona_text[:1500] + "\n\n" if persona_text else "") + prompt_tpl
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
        text = await self._gen(self.tpl_random)
        try:
            sid = str(event.get_sender_id())
            pid = str(event.unified_msg_origin).split(":")[0]  # 发送者所在平台
            await self.context.send_message(
                f"{pid}:FriendMessage:{sid}", MessageChain().message(text)
            )
            yield event.plain_result(f"已私聊发送：{text[:60]}（测试不计入当日频控）")
        except Exception:
            yield event.plain_result("发送失败，看日志")
