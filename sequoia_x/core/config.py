"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"
    feishu_webhook_url: str  # 必填字段，缺失时抛出 ValidationError
    strategy_webhooks: dict[str, str] = {}
    #: 策略读取的行情窗口（交易日数）。下限 62：60 日均线策略需要 61 根 K 线。
    snapshot_days: int = Field(default=130, ge=62)
    #: 行情同步的并发进程数。8 个并发登录容易触发 baostock 限流，默认降到 4。
    sync_workers: int = Field(default=4, ge=1, le=16)
    #: 行情同步的时间预算（秒）。到点即收工，用已有数据继续选股，避免网络故障时挂死。
    #: 实测 4 进程下每只股票约 0.15~0.2 s，全市场一轮约 3~4 分钟。
    sync_max_seconds: float = Field(default=240.0, gt=0)
    #: 是否按板块判定涨跌停（创业板/科创板 20%、北交所 30%）。关闭则沿用旧的 9.5%。
    board_aware_limits: bool = True

    # ── 选股通用过滤（见 strategy/filters.py）──
    #: 最小成交额（元）。低于此值的票流动性差、滑点大
    min_turnover: float = Field(default=50_000_000.0, ge=0)
    #: 最小股价（元）。剔除仙股
    min_price: float = Field(default=2.0, ge=0)
    #: 快照窗口内最少的 K 线根数。低于此值视为次新/长期停牌
    min_history_bars: int = Field(default=120, ge=3)
    #: 每个策略最多输出的股票数（按分数取前 N）。0 表示不限制
    max_picks: int = Field(default=30, ge=0)
    #: 是否剔除 ST / *ST / 退市股
    exclude_st: bool = True

    # ── 运维告警（P2-7）──
    #: 告警专用 Webhook（可独立于选股群）。留空则告警只写日志，不推送
    alert_webhook_url: str | None = None
    #: 数据新鲜度容忍：行情最新日期距"应有交易日"最多落后几天
    max_data_lag_days: int = Field(default=1, ge=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。"""
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix) :].lower()
                webhooks[strategy_key] = value

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到则 fallback 到 feishu_webhook_url。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。
    若必填字段（feishu_webhook_url）缺失，抛出 pydantic_core.ValidationError。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
