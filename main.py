import asyncio
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import MessageMember, MessageType
from astrbot.api.star import Star, StarTools, register
from astrbot.core.provider.entities import ProviderType


@register("group_mate", "WinBSOD", "提供有趣的群友社交功能", "1.4")
class GroupMatePlugin(Star):
    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 并发控制：异步锁仅用于保护数据读写状态，严禁在锁内执行长耗时/网络 I/O
        self.lock = asyncio.Lock()

        # 规范数据目录获取使用 StarTools.get_data_dir()
        # 完全拥抱面向对象的 pathlib，避免 Path 与 str 混用
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "data.json"
        self.data = self._load_data()

    def _load_data(self) -> dict:
        """从持久化文件加载运行数据并进行结构校验"""
        default_data = {"last_run_time": {}}
        if self.data_file.exists():
            try:
                with open(self.data_file, encoding="utf-8") as f:
                    loaded = json.load(f)

                # 数据结构校验与清洗：防止脏数据导致运行期异常
                if not isinstance(loaded, dict):
                    raise ValueError("JSON 根对象不是字典")

                lrt = loaded.get("last_run_time")
                if not isinstance(lrt, dict):
                    loaded["last_run_time"] = {}
                else:
                    # 清洗 last_run_time，确保 key 为字符串，value 为数值
                    cleaned_lrt = {}
                    for k, v in lrt.items():
                        if isinstance(v, (int, float)):
                            cleaned_lrt[str(k)] = v
                    loaded["last_run_time"] = cleaned_lrt

                return loaded
            except json.JSONDecodeError as e:
                logger.error(f"[group_mate] 数据文件格式损坏，将进行备份并重置: {e}")
                try:
                    # 发现损坏时备份原文件，防止 _save_data 覆盖导致数据永久丢失
                    backup_path = self.data_file.with_suffix(".json.bak")
                    self.data_file.rename(backup_path)
                    logger.info(f"[group_mate] 已将损坏的数据文件备份至: {backup_path}")
                except Exception as b_e:
                    logger.error(f"[group_mate] 备份失败: {b_e}")
            except Exception as e:
                logger.error(f"[group_mate] 加载数据遇到异常: {e}")
        return default_data

    async def _save_data(self):
        """保存运行数据到持久化文件（需在外部 lock 保护下调用）"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[group_mate] 保存数据失败: {e}")

    def _get_conf(self, category: str, key: str, default=None):
        """层级化配置读取，包含鲁棒性校验"""
        val = self.config.get(category, {}).get(key, default)

        # 配置项鲁棒性校验：确保概率值在合法区间 [0, 100]
        if category == "basic_settings" and key == "random_fail_probability":
            try:
                val = float(val)
                return max(0.0, min(100.0, val))
            except (ValueError, TypeError):
                return 5.0
        return val

    def _get_cd_key(self, event: AstrMessageEvent) -> str:
        """生成支持多群/多平台隔离的冷却唯一标识"""
        platform = event.get_platform_id()
        group = event.get_group_id()
        user = event.get_sender_id()
        return f"{platform}:{group}:{user}"

    def _is_in_cooldown(self, cd_key: str) -> tuple[bool, int]:
        """检查特定标识是否处于指令冷却期（辅助方法，应在 lock 保护下调用）"""
        cd = self._get_conf("basic_settings", "cooldown", 60)
        now = time.time()
        lrt_dict = self.data.get("last_run_time", {})
        if cd_key in lrt_dict:
            elapsed = now - lrt_dict[cd_key]
            if elapsed < cd:
                return True, int(cd - elapsed)
        return False, 0

    async def _get_ai_response(self, category: str, **kwargs) -> str:
        """核心回复获取逻辑，具备超时控制与 Providers 状态预检"""
        DEFAULTS = {
            "success_result": {
                "prompt": "你是一个幽默且擅长牵红线的月老。现在用户 {user} 抽中了群友 {target} 作为今日老婆。请生成一两句恭喜或调侃的话，要简短精炼，不要输出多余的内容。",
                "fixed": [
                    "这就是你的今日真命天子/天女吗？",
                    "恭喜你，和这位群友结缘了！",
                    "缘分天注定，快去看看你的新老婆吧！",
                ],
            },
            "random_fail": {
                "prompt": "你是一个毒舌且幽默的月老。用户试图抽老婆但由于运气极差失败了。请生成一句话嘲笑他“单身一辈子”或者“醒醒吧”，要简短精炼。",
                "fixed": [
                    "醒醒，你根本没有老婆！",
                    "月老看你太可怜，都不想给你牵线了。",
                    "今天的桃花运被隔壁王大爷借走了。",
                ],
            },
            "cooldown_remind": {
                "prompt": "你是一个毒舌且幽默的机器人。用户在试图频繁使用“娶群友”指令，现在正处于冷却中。请生成一句话来调侃下他，回复需要很简短精炼。",
                "fixed": [
                    "心急吃不了热豆腐，请在 {remain} 秒后再尝试。",
                    "别急啊，月老也需要休息的。",
                    "频率太快了，你是想把月老的红线搓出火星子吗？",
                ],
            },
            "not_group_remind": {
                "prompt": "你是一个高冷的管家。用户在私聊中试图使用群功能。请生成一句话告诉他这需要在群里玩，带点调侃，不要太死板。",
                "fixed": [
                    "该功能仅限群聊使用，私聊可找不到老婆哦。",
                    "去群里展示你的桃花运吧，这里没人看。",
                    "私聊禁止“娶群友”，请自重。",
                ],
            },
            "system_error_remind": {
                "prompt": "你是一个倒霉的程序员。现在插件出 Bug 了。请生成一句话自嘲并请用户稍后再试，要简短。",
                "fixed": [
                    "哎呀，月老把红线扯断了...请稍后再试。",
                    "系统开小差了，桃花运暂时离你而去了。",
                    "服务器正在重启中，请不要在危险边缘试探。",
                ],
            },
        }

        conf_group = self.config.get(category, {})
        category_default = DEFAULTS.get(
            category, {"prompt": "", "fixed": ["月老去喝茶了。"]}
        )

        use_llm = conf_group.get("mode", True)
        prompt_template = conf_group.get("prompt") or category_default["prompt"]
        fixed_sentences = conf_group.get("fixed") or category_default["fixed"]

        def safe_format(text: str, vars: dict) -> str:
            try:
                return text.format(**vars)
            except Exception as e:
                logger.error(f"[group_mate] 变量格式化失败 ({category}): {e}")
                # 安全兜底：如果格式化失败，返回固定语库中的第一条，防止暴露原始模板占位符
                return fixed_sentences[0] if fixed_sentences else "缘分莫测，稍后再试。"

        if use_llm:
            provider = self.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION
            )
            if not provider:
                return safe_format(random.choice(fixed_sentences), kwargs)

            retry_limit = self._get_conf("basic_settings", "llm_retry_limit", 2)
            timeout = self._get_conf(
                "basic_settings", "llm_timeout", 10
            )  # 增加请求超时控制

            for attempt in range(retry_limit + 1):
                try:
                    provider = self.context.provider_manager.get_using_provider(
                        ProviderType.CHAT_COMPLETION
                    )
                    if not provider:
                        break

                    full_prompt = safe_format(prompt_template, kwargs)
                    # 引入 asyncio.wait_for 防止接口挂起拖累插件
                    response = await asyncio.wait_for(
                        provider.text_chat(prompt=full_prompt), timeout=timeout
                    )
                    if response and response.completion_text:
                        return response.completion_text.strip()
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[group_mate] LLM 请求超时 ({category})，尝试重试..."
                    )
                except Exception as e:
                    logger.warning(
                        f"[group_mate] LLM 调用尝试 {attempt + 1} 失败 ({category}): {e}"
                    )

                if attempt < retry_limit:
                    await asyncio.sleep(1)
            return safe_format(random.choice(fixed_sentences), kwargs)
        return safe_format(random.choice(fixed_sentences), kwargs)

    async def _select_target(
        self, event: AstrMessageEvent, user_id: str
    ) -> MessageMember | None:
        """从群成员中根据活跃度挑选目标，优化了时区处理与类型标注"""
        group_id = event.get_group_id()
        platform_id = event.get_platform_id()
        history_size = self._get_conf("basic_settings", "history_size", 200)

        # 1. 获取群消息历史
        try:
            history = await self.context.message_history_manager.get(
                platform_id, group_id, page_size=history_size
            )
        except Exception as e:
            logger.error(f"[group_mate] 获取群消息历史失败: {e}")
            history = []

        # 2. 活跃度分析与严谨的时区处理
        now_dt = datetime.now(timezone.utc)
        activity_counts = {}
        last_seen = {}

        for msg_item in history:
            uid = msg_item.sender_id
            if uid:
                activity_counts[uid] = activity_counts.get(uid, 0) + 1
                msg_time = msg_item.created_at
                if msg_time:
                    # 对于 Naive Datetime，显式使用 replace 赋予 UTC 时区（假设框架返回的是 UTC naive）
                    # 若需本地时间转换，应先 replace(本地) 再 astimezone(UTC)
                    if msg_time.tzinfo is None:
                        msg_time = msg_time.replace(tzinfo=timezone.utc)
                    else:
                        msg_time = msg_time.astimezone(timezone.utc)

                    if uid not in last_seen or msg_time > last_seen[uid]:
                        last_seen[uid] = msg_time

        # 3. 获取群成员
        try:
            group_obj = await event.get_group()
        except Exception as e:
            logger.error(f"[group_mate] 获取群成员列表失败: {e}")
            return None

        if not group_obj or not group_obj.members:
            return None

        members = [m for m in group_obj.members if m.user_id != user_id]
        if not members:
            return None

        # 4. 分层逻辑计算
        target_pool = members
        if self._get_conf("basic_settings", "use_multi_tier_fallback", True):
            tier1, tier2 = [], []
            for m in members:
                m_last_time = last_seen.get(m.user_id)
                if m_last_time:
                    diff = (now_dt - m_last_time).total_seconds()
                    if diff <= 3 * 86400:
                        tier1.append(m)
                    if diff <= 7 * 86400:
                        tier2.append(m)
            if tier1:
                target_pool = tier1
            elif tier2:
                target_pool = tier2

        weights = [activity_counts.get(m.user_id, 0) + 1 for m in target_pool]
        return random.choices(target_pool, weights=weights, k=1)[0]

    @filter.command("娶群友")
    async def marry(self, event: AstrMessageEvent):
        """娶一位群友作为老婆/老公。指令状态控制与耗时操作分离以确保吞吐量。"""
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                msg = await self._get_ai_response("not_group_remind")
                yield event.plain_result(msg)
                return

            user_id = event.get_sender_id()
            user_name = event.get_sender_name()
            cd_key = self._get_cd_key(event)

            # --- 将网络 I/O 移出锁区域 ---
            # 1. 第一阶段逻辑锁定（轻量级）
            async with self.lock:
                in_cd, remain = self._is_in_cooldown(cd_key)
                if in_cd:
                    # 锁内仅做判定，具体 AI 请求移至锁外
                    pass
                else:
                    # 随机失败预判定
                    prob = (
                        self._get_conf("basic_settings", "random_fail_probability", 5.0)
                        / 100.0
                    )
                    is_failed = prob > 0 and random.random() < prob
                    if is_failed:
                        # 立即标记冷却，防止并发绕过
                        self.data.setdefault("last_run_time", {})[cd_key] = time.time()
                        await self._save_data()
                    else:
                        # 正常流程预标记，随后再释放锁进行耗时计算
                        self.data.setdefault("last_run_time", {})[cd_key] = time.time()
                        await self._save_data()

            # 2. 第二阶段耗时操作（锁外执行）
            if in_cd:
                msg = await self._get_ai_response(
                    "cooldown_remind", user=user_name, remain=remain
                )
                yield event.plain_result(msg)
                return

            if is_failed:
                msg = await self._get_ai_response("random_fail", user=user_name)
                yield event.plain_result(msg)
                return

            # 选定目标（包含群成员/历史拉取，耗时较高）
            target = await self._select_target(event, user_id)
            if not target:
                msg = await self._get_ai_response("system_error_remind")
                yield event.plain_result(msg)
                return

            success_msg = await self._get_ai_response(
                "success_result",
                user=user_name,
                target=target.nickname or target.user_id,
            )

            chain = [
                At(qq=user_id),
                Plain(" "),
                Plain("\n"),
                Plain(f"{success_msg}\n"),
                Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&s=0&nk={target.user_id}"),
                Plain(f"\n✨ 【{target.nickname or '神秘群友'}】({target.user_id})"),
            ]
            yield event.chain_result(chain)

        except Exception as e:
            logger.error(f"[group_mate] 运行异常: {e}", exc_info=True)
            msg = await self._get_ai_response(
                "system_error_remind", user=event.get_sender_name()
            )
            yield event.plain_result(msg)

    @filter.command("gm_admin")
    async def gm_admin(
        self, event: AstrMessageEvent, action: str = None, value: str = None
    ):
        """管理员控制台指令。"""
        if not event.is_admin():
            yield event.plain_result("❌ 权限不足，该指令仅限管理员使用。")
            return

        mapping = {
            "success": ("success_result", "mode"),
            "fail": ("random_fail", "mode"),
            "cd": ("cooldown_remind", "mode"),
            "privacy": ("not_group_remind", "mode"),
            "error": ("system_error_remind", "mode"),
            "fallback": ("basic_settings", "use_multi_tier_fallback"),
        }

        if not action or (action not in mapping and action != "prob") or value is None:
            help_msg = (
                "💡 【星洛智萌_群友眷属 管理员控制台】\n用法: /gm_admin [项目] [on/off/数值]\n\n"
                "项目: success, fail, cd, privacy, error, fallback (on/off)\n特殊: prob (0-100)\n\n当前状态:\n"
            )
            for k, (cat, key) in mapping.items():
                status = "ON" if self._get_conf(cat, key, True) else "OFF"
                help_msg += f"- {k}: {status}\n"
            help_msg += f"- prob: {self._get_conf('basic_settings', 'random_fail_probability', 5.0)}%\n"
            yield event.plain_result(help_msg)
            return

        target_val = None
        if action == "prob":
            try:
                target_val = float(value)
                if not (0 <= target_val <= 100):
                    yield event.plain_result("❌ 概率必须在 0 到 100 之间。")
                    return
            except ValueError:
                yield event.plain_result("❌ 请输入有效数字。")
                return
        else:
            val_norm = value.lower()
            if val_norm in ["on", "true", "1"]:
                target_val = True
            elif val_norm in ["off", "false", "0"]:
                target_val = False
            else:
                yield event.plain_result(f"❌ 非法布尔值: {value}。请使用 on/off。")
                return

        cat, key = (
            mapping[action]
            if action != "prob"
            else ("basic_settings", "random_fail_probability")
        )
        if cat not in self.config:
            self.config[cat] = {}
        self.config[cat][key] = target_val
        self.context.config_manager.save_config()
        yield event.plain_result(f"✅ 已将 {action} 设置为 {target_val}，且已持久化。")
