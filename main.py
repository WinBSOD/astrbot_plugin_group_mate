import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

from astrbot.api.all import *
from astrbot.core.provider.entities import ProviderType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")


@register("group_mate", "Antigravity", "提供有趣的群友社交功能", "1.2.1")
class GroupMatePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 【原则 3】持久化数据存储于 AstrBot 统一的 data 目录下
        self.data_dir = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_group_mate"
        )
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        self.data_file = os.path.join(self.data_dir, "data.json")
        self.data = self._load_data()

    def _load_data(self) -> dict:
        """从持久化文件加载运行数据"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                # 【原则 4】良好的错误处理
                logger.error(f"[group_mate] 加载数据失败: {e}")
        return {"last_run_time": {}}

    def _save_data(self):
        """保存运行数据到持久化文件"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[group_mate] 保存数据失败: {e}")

    def _get_conf(self, category: str, key: str, default=None):
        """通用层级化配置读取辅助函数"""
        return self.config.get(category, {}).get(key, default)

    def is_in_cooldown(self, user_id: str) -> tuple[bool, int]:
        """检查用户是否处于指令冷却期"""
        cd = self._get_conf("basic_settings", "cooldown", 60)
        now = time.time()
        last_run_time = self.data.get("last_run_time", {})
        if user_id in last_run_time:
            elapsed = now - last_run_time[user_id]
            if elapsed < cd:
                return True, int(cd - elapsed)
        return False, 0

    async def get_ai_response(self, category: str, **kwargs) -> str:
        """
        核心回复获取逻辑：
        1. 根据开关选择 AI 生成或固定话术。
        2. AI 模式下支持 Prompt 自定义与多变量注入。
        3. 【原则 4/6】具备重试机制与异步非阻塞调用，AI 失败自动回滚至固定话术库。
        """
        # 预设默认 Prompts 与固定话术 (与 _conf_schema.json 同步)
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

        def safe_format(text: str, vars: dict):
            """安全字符串格式化，防止变量缺失导致崩溃"""
            try:
                return text.format(**vars)
            except Exception as e:
                logger.error(f"[group_mate] 变量格式化失败 ({category}): {e}")
                return text

        if use_llm:
            if not prompt_template:
                return safe_format(random.choice(fixed_sentences), kwargs)

            # AI 重试逻辑，确保高可用
            retry_limit = self._get_conf("basic_settings", "llm_retry_limit", 2)
            for attempt in range(retry_limit + 1):
                try:
                    provider = self.context.provider_manager.get_using_provider(
                        ProviderType.CHAT_COMPLETION
                    )
                    if provider:
                        full_prompt = safe_format(prompt_template, kwargs)
                        # 【原则 6】使用 AstrBot 内置异步接口进行 AI 对话
                        response = await provider.text_chat(prompt=full_prompt)
                        if response and response.completion_text:
                            return response.completion_text.strip()
                except Exception as e:
                    logger.warning(
                        f"[group_mate] LLM 调用尝试 {attempt + 1} 失败 ({category}): {e}"
                    )
                    if attempt < retry_limit:
                        await asyncio.sleep(1)

            # AI 最终失效后回滚至固定话术池
            return safe_format(random.choice(fixed_sentences), kwargs)
        else:
            # 显式关闭 AI 模式后使用固定话术
            return safe_format(random.choice(fixed_sentences), kwargs)

    @command(
        "娶群友",
        alias={
            "谁是我老婆",
            "绑架群友",
            "拐群友",
            "哪个群友是我老婆",
            "娶老婆",
            "拐卖人口",
            "抽老婆",
            "拐走群友",
            "拐卖群友",
            "绑架人口",
        },
    )
    async def marry(self, event: AstrMessageEvent):
        """娶一位群友作为老婆/老公。主业务逻辑。"""
        try:
            # 1. 环境验证：仅限群聊
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                msg = await self.get_ai_response("not_group_remind")
                yield event.plain_result(msg)
                return

            user_id = event.get_sender_id()
            user_name = event.get_sender_name()

            # 2. 冷却检查
            in_cd, remain = self.is_in_cooldown(user_id)
            if in_cd:
                msg = await self.get_ai_response(
                    "cooldown_remind", user=user_name, remain=remain
                )
                yield event.plain_result(msg)
                return

            # 3. 随机落空机制 (可调概率)
            prob = (
                self._get_conf("basic_settings", "random_fail_probability", 5.0) / 100.0
            )
            if prob > 0 and random.random() < prob:
                self.data.setdefault("last_run_time", {})[user_id] = time.time()
                self._save_data()
                msg = await self.get_ai_response("random_fail", user=user_name)
                yield event.plain_result(msg)
                return

            # 4. 获取历史活跃数据（回溯算法）
            group_id = event.get_group_id()
            platform_id = event.get_platform_id()
            history_size = self._get_conf("basic_settings", "history_size", 200)

            try:
                history = await self.context.message_history_manager.get(
                    platform_id, group_id, page_size=history_size
                )
            except Exception as e:
                logger.error(f"[group_mate] 获取群消息历史失败: {e}")
                history = []

            now_dt = datetime.now(timezone.utc)
            activity_counts = {}
            last_seen = {}

            # 解析消息历史，构建用户活跃画像
            for msg_item in history:
                uid = msg_item.sender_id
                if uid:
                    activity_counts[uid] = activity_counts.get(uid, 0) + 1
                    msg_time = msg_item.created_at
                    if uid not in last_seen or msg_time > last_seen[uid]:
                        last_seen[uid] = msg_time

            # 5. 获取群成员列表并进行多维筛选
            try:
                group_obj = await event.get_group()
            except Exception as e:
                logger.error(f"[group_mate] 获取群成员列表失败: {e}")
                msg = await self.get_ai_response("system_error_remind")
                yield event.plain_result(msg)
                return

            if not group_obj or not group_obj.members:
                msg = await self.get_ai_response("system_error_remind")
                yield event.plain_result(msg)
                return

            # 过滤掉发起者本人
            members = [m for m in group_obj.members if m.user_id != user_id]
            if not members:
                msg = await self.get_ai_response("system_error_remind")
                yield event.plain_result(msg)
                return

            # 多级活跃回溯算法：优先抽取 3 天/7 天内活跃的成员
            target_pool = members
            if self._get_conf("basic_settings", "use_multi_tier_fallback", True):
                tier1 = []
                tier2 = []
                for m in members:
                    m_last_time = last_seen.get(m.user_id)
                    if m_last_time:
                        if not isinstance(m_last_time, datetime):
                            m_last_time = datetime.fromtimestamp(
                                m_last_time, tz=timezone.utc
                            )
                        diff = (now_dt - m_last_time).total_seconds()
                        if diff <= 3 * 86400:
                            tier1.append(m)
                        if diff <= 7 * 86400:
                            tier2.append(m)

                if tier1:
                    target_pool = tier1
                elif tier2:
                    target_pool = tier2

            # 活跃度加权算法：发言越多，权重越高（+1 偏移防止零权重）
            weights = []
            for m in target_pool:
                weight = activity_counts.get(m.user_id, 0) + 1
                weights.append(weight)

            # 6. 执行加权抽取
            target = random.choices(target_pool, weights=weights, k=1)[0]

            # 7. 更新冷却数据并保存
            self.data.setdefault("last_run_time", {})[user_id] = time.time()
            self._save_data()

            # 8. 生成回复并组合消息链
            success_msg = await self.get_ai_response(
                "success_result",
                user=user_name,
                target=target.nickname or target.user_id,
            )

            # 组合消息链：At + 空格 + 换行 + 正文 + 头像 + 详情
            chain = [
                At(qq=user_id),
                Plain(" "),  # 显式空格，适配多平台显示
                Plain("\n"),  # 强制换行
                Plain(f"{success_msg}\n"),
                Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&s=0&nk={target.user_id}"),
                Plain(f"\n✨ 【{target.nickname or '神秘群友'}】({target.user_id})"),
            ]
            yield event.chain_result(chain)

        except Exception as e:
            # 【原则 4】顶层异常捕获，防止插件崩溃导致 AstrBot 退出
            logger.error(f"[group_mate] 运行异常: {e}", exc_info=True)
            msg = await self.get_ai_response(
                "system_error_remind", user=event.get_sender_name()
            )
            yield event.plain_result(msg)

    @command("gm_admin")
    async def gm_admin(
        self, event: AstrMessageEvent, action: str = None, value: str = None
    ):
        """管理员指令：管理插件功能。用法: /gm_admin [功能项] [参数]"""
        if not event.is_admin():
            yield event.plain_result("❌ 权限不足，该指令仅限管理员使用。")
            return

        # 配置项隐式映射
        mapping = {
            "success": ("success_result", "mode"),
            "fail": ("random_fail", "mode"),
            "cd": ("cooldown_remind", "mode"),
            "privacy": ("not_group_remind", "mode"),
            "error": ("system_error_remind", "mode"),
            "fallback": ("basic_settings", "use_multi_tier_fallback"),
        }

        # 帮助菜单与状态看板
        if not action or (action not in mapping and action != "prob") or not value:
            help_msg = "💡 【群友助手管理员控制台】\n用法: /gm_admin [项目] [参数]\n\n"
            help_msg += "项目: success, fail, cd, privacy, error, fallback (on/off)\n"
            help_msg += "特殊: prob (0-100)\n\n"
            help_msg += "当前状态:\n"
            for k, (cat, key) in mapping.items():
                status = "ON" if self._get_conf(cat, key, True) else "OFF"
                help_msg += f"- {k}: {status}\n"
            help_msg += f"- prob: {self._get_conf('basic_settings', 'random_fail_probability', 5.0)}%\n"
            yield event.plain_result(help_msg)
            return

        # 概率快速设置逻辑
        if action == "prob":
            try:
                p = float(value)
                if 0 <= p <= 100:
                    if "basic_settings" not in self.config:
                        self.config["basic_settings"] = {}
                    self.config["basic_settings"]["random_fail_probability"] = p
                    yield event.plain_result(f"✅ 已将随机落空概率设置为 {p}%。")
                else:
                    yield event.plain_result("❌ 概率必须在 0 到 100 之间。")
            except ValueError:
                yield event.plain_result("❌ 请输入有效数字。")
            return

        # 布尔开关逻辑
        v_bool = value.lower() == "on"
        cat, key = mapping[action]
        if cat not in self.config:
            self.config[cat] = {}
        self.config[cat][key] = v_bool
        yield event.plain_result(
            f"✅ 已将 {action} 设置为 {'开启 (ON)' if v_bool else '关闭 (OFF)'}。"
        )
