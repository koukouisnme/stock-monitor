"""领域层共享模型（纯数据，无IO）。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TurnResult:
    """九转计算结果。count: 正=上涨结构(顶部九转), 负=下跌结构(底部九转)"""
    count: int = 0                      # 当前计数（带符号）
    structure_complete: bool = False     # 是否恰好完成9
    perfected: bool = False             # 8/9极值验证（DeMark perfection）
    history: list = field(default_factory=list)  # 全序列带符号计数

    @property
    def direction(self) -> str:
        return "up" if self.count > 0 else ("down" if self.count < 0 else "")


@dataclass
class VolumeProfile:
    """量能画像。"""
    vol_ratio: float = 0.0          # 当日量/5日均量
    vol_ratio_period: float = 0.0   # 周期量比
    amt_ratio: float = 0.0          # 额比：当日额/5日均额
    amount: float = 0.0
    volume_percentile: float = 0.0  # 当日量在历史窗口的分位
    is_surge: bool = False
    surge_type: str = ""            # up/down/stagnant/shrink
    models: dict = field(default_factory=dict)  # 命中的量价模型 1-4


@dataclass
class SignalResult:
    """信号融合输出。"""
    code: str = ""
    name: str = ""
    level: str = "C"                 # S/A/B/C
    action: str = "hold"             # buy/sell/hold
    score: int = 0
    reasons: list = field(default_factory=list)   # 逐层过滤通过/得分说明
    position_ratio: float = 0.0      # 建议仓位
    stop_loss: Optional[float] = None
    ref_price: float = 0.0
    turn: int = 0
    period: str = "day"
    trade_date: str = ""                # 信号触发交易日（跟踪与展示用）


@dataclass
class LOFState:
    """LOF/ETF 溢价状态。"""
    code: str = ""
    name: str = ""
    price: float = 0.0
    trade_date: str = ""             # 快照交易日（溢价历史落库用）
    nav_official_est: float = 0.0    # 官方口径估算净值（中间价汇率）
    nav_reference_est: float = 0.0   # 参考口径估算净值（离岸汇率）
    nav_source: str = "estimate"     # iopv/estimate/official
    premium_official: float = 0.0    # 实时口径：价 vs 实时估算净值(IOPV>估算>昨净值降级)
    premium_reference: float = 0.0
    premium_t1: float = 0.0          # T-1口径：价 vs 昨官方净值（不含当日底层变动）
    premium_percentile: float = 0.5
    share_chg_pct: float = 0.0
    spread_gap: float = 0.0          # 双口径差（不确定性）
    note: str = ""
