# ==============================================================================
# 🎯 [部署节点] : Quant-PC
# 📦 [环境依赖] : math, time (纯原生，0 依赖，追求极限执行速度)
# 🔗 [职责]     : O(1) 复杂度的 Hawkes 脉冲状态机 (微秒级响应)
# ==============================================================================
import math

class FastHawkesEngine:
    """
    极速 Hawkes 脉冲探测器 (Marked Hawkes Process)
    采用 O(1) 指数衰减递归算法，专为 Tick 级高频交易设计。
    """
    def __init__(self, mu=1.0, alpha=1.5, beta=1.2, volume_threshold=500, trigger_level=25.0):
        # 物理参数
        self.mu = mu                # 基础背景噪音 (市场平淡时的冰点热度)
        self.alpha = alpha          # 激震系数（因使用平方根压缩，必须放大到 1.0 以上才有足够灵敏度）
        self.beta = beta            # 衰减速率（1.2 衰减很快，符合 ETF 实测余震周期）
        self.vol_threshold = volume_threshold # 大单门槛（手），低于此值的散单直接忽略
        self.trigger_level = trigger_level    # 🚨 开火阈值

        # 🚨 做市商过滤网：单笔超过此值视为机构对倒/ETF申赎延迟播报，拒绝计入动能
        # 物理依据：真实游资扫盘不会在单 tick 出现数万手；这类巨单通常在内部撮合，
        # 不会吃掉盘口流动性，不产生真实 FOMO 余震
        self.whale_cap_limit = 20000

        # 极速状态机内存 (O(1) 核心)
        self.last_time = 0.0
        self.decay_sum = 0.0        # 历史衰减动能池（基于 sqrt(volume) 的 impact 累积）
        
    def process_tick(self, timestamp_sec: float, price: float, volume: int, buy_flag: int) -> dict:
        """
        处理单个 Tick (要求外部传入的是干净的、按时间排序的 Tick)
        :param timestamp_sec: 当前 Tick 的时间戳 (秒级浮点数，如 1618300000.123)
        :param price: 最新价
        :param volume: 单笔成交量 (手)
        :param buy_flag: 1 为主动买入 (外盘/吃卖单)，-1 为主动卖出 (内盘)
        """
        # 1. 三重门卫过滤
        #    - 非主动买单：直接忽略（不计入动能，但时间流逝使 λ 自然衰减）
        #    - 低于门槛的散单：同上
        #    - 超过 whale_cap_limit 的巨单：做市商对倒/ETF申赎延时播报，物理拒绝
        if buy_flag != 1 or volume < self.vol_threshold or volume > self.whale_cap_limit:
            current_lambda = self._calculate_current_lambda(timestamp_sec)
            return {"fire": False, "lambda": current_lambda}

        # 2. 计算距离上一次激震点的时间差
        if self.last_time == 0.0:
            delta_t = 0.0
        else:
            delta_t = timestamp_sec - self.last_time
            if delta_t < 0: delta_t = 0.0  # 防御乱序 Tick

        # 3. 💣 O(1) 极速递归计算核心（平方根法则物理手术）
        #
        #    旧版（线性累加）：decay_sum += volume
        #    缺陷：87000手 vs 870手 的动能比为 100:1，导致极端大单炸穿 λ 后
        #          随后几分钟的自然衰减漫长，连环假信号。
        #
        #    新版（平方根市场冲击法则）：decay_sum += sqrt(volume)
        #    物理依据：订单对市场的真实冲击服从平方根法则（Square Root Law of Market Impact）
        #    87000 手 → sqrt(87000) ≈ 295；870 手 → sqrt(870) ≈ 29.5
        #    比例从 100:1 压缩至 10:1，极端胖尾得到物理降维
        decay_factor = math.exp(-self.beta * delta_t)
        impact_volume = math.sqrt(volume)  # 平方根压缩：量纲从「手」降维至「冲击单位」
        self.decay_sum = self.decay_sum * decay_factor + impact_volume

        # 4. 更新激震时间戳
        self.last_time = timestamp_sec

        # 5. 计算当前绝对强度 λ(t)
        current_lambda = self.mu + self.alpha * self.decay_sum

        # 6. 刺客判决
        is_fire = current_lambda >= self.trigger_level

        return {
            "fire": is_fire,
            "lambda": round(current_lambda, 3),
            "price": price
        }

    def _calculate_current_lambda(self, current_time_sec: float) -> float:
        """用于获取当前自然衰减后的实时强度 (不增加新动能)"""
        if self.last_time == 0.0: return self.mu
        delta_t = current_time_sec - self.last_time
        if delta_t < 0: return self.mu + self.alpha * self.decay_sum
        current_decay_sum = self.decay_sum * math.exp(-self.beta * delta_t)
        return self.mu + self.alpha * current_decay_sum

# ==============================================================================
# 本地靶场测试 (确保引擎逻辑 0 误差)
# ==============================================================================
if __name__ == "__main__":
    import sys as _sys
    if _sys.stdout.encoding and _sys.stdout.encoding.lower() != 'utf-8':
        try: _sys.stdout.reconfigure(encoding='utf-8')
        except AttributeError: pass

    print("=" * 60)
    print("Hawkes Engine v2 — 平方根法则 + 做市商过滤网 压测")
    print("=" * 60)
    engine = FastHawkesEngine()  # 使用新默认参数：alpha=1.5, beta=1.2, trigger=25, whale=20000

    # 场景一：连续真实扫单（应触发开火）
    print("\n[场景一] 主力连续真实扫单（每笔 1000~5000 手）")
    ticks_real = [
        (10.0, 1.332,   300, 1),   # 散单，低于门槛 500，忽略
        (10.5, 1.332,  1200, 1),   # sqrt(1200)≈34.6 → decay_sum=34.6 → λ=1+1.5*34.6=52.9 → 开火！
        (11.0, 1.332,  2000, 1),
        (11.5, 1.332,  3000, 1),
        (15.0, 1.332,   400, 1),   # 几秒后热度衰减，散单低于门槛
    ]
    for t, p, v, flag in ticks_real:
        res = engine.process_tick(t, p, v, flag)
        tag = "🚨 开火！" if res['fire'] else "蛰伏..."
        print(f"  [{t:5.1f}s] vol={v:6d}手  lambda={res['lambda']:8.3f}  {tag}")

    # 场景二：做市商巨单（应被 whale_cap 拦截，λ 不变）
    print("\n[场景二] 做市商对倒/ETF申赎巨单（单笔 8 万手，应被拒绝）")
    engine2 = FastHawkesEngine()
    whale_ticks = [
        (10.0, 1.332, 87121, 1),   # 超过 whale_cap=20000，物理拒绝
        (10.3, 1.332, 81526, 1),   # 同上
        (10.6, 1.332,  1500, 1),   # 真实大单，正常计入
    ]
    for t, p, v, flag in whale_ticks:
        res = engine2.process_tick(t, p, v, flag)
        tag = "🚨 开火！" if res['fire'] else "蛰伏..."
        block = " [WHALE拒绝]" if v > engine2.whale_cap_limit else ""
        print(f"  [{t:5.1f}s] vol={v:6d}手{block:12s}  lambda={res['lambda']:8.3f}  {tag}")