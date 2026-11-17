"""实验分析使用的纯标准库统计函数。

只实现本工作台需要的两项能力：
1. 学生 t 分布的累积分布与分位数（基于正则化不完全贝塔函数）；
2. 两臂聚类层面均值差的 CR1（Liang-Zeger 小样本校正）三明治方差。

不引入第三方依赖，所有输入均为 Python 内置数值类型。
"""
import math


def betai(a, b, x):
    """正则化不完全贝塔函数 I_x(a, b)，Numerical Recipes 连分式实现。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _betacf(a, b, x):
    max_iter = 200
    epsilon = 3.0e-14
    fp_min = 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fp_min:
        d = fp_min
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fp_min:
            d = fp_min
        c = 1.0 + aa / c
        if abs(c) < fp_min:
            c = fp_min
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fp_min:
            d = fp_min
        c = 1.0 + aa / c
        if abs(c) < fp_min:
            c = fp_min
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def t_cdf(t, df):
    """学生 t 分布累积分布函数。"""
    x = df / (df + t * t)
    tail = 0.5 * betai(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def t_quantile(p, df):
    """学生 t 分布分位数，二分求解；df 为正，p 取 (0,1)。"""
    if not 0.0 < p < 1.0:
        raise ValueError('分位概率须在 (0,1) 内')
    if df <= 0:
        raise ValueError('自由度须为正')
    lo, hi = -1.0, 1.0
    while t_cdf(lo, df) > p:
        lo *= 2.0
    while t_cdf(hi, df) < p:
        hi *= 2.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def cluster_mean_diff(clusters):
    """两臂聚类均值差与 CR1 聚类稳健标准误。

    clusters 为 (arm, value) 序列，arm 取 1=活动、0=对照；每个聚类贡献一个
    value（例如该机会内人均结果）。返回 None 表示聚类数不足以辨认。
    """
    activity = [value for arm, value in clusters if arm == 1]
    control = [value for arm, value in clusters if arm == 0]
    n1, n0 = len(activity), len(control)
    if n1 < 2 or n0 < 2:
        return None
    mean1 = sum(activity) / n1
    mean0 = sum(control) / n0
    total = n1 + n0
    # OLS 含截距、处理虚拟变量下的肉矩阵 B = Σ x_j x_j' e_j²
    b00 = sum((value - mean0) ** 2 for value in control)
    b00 += sum((value - mean1) ** 2 for value in activity)
    b10 = sum((value - mean1) ** 2 for value in activity)
    denom = (n1 * n0) ** 2
    variance_cr0 = (n1 ** 2 * b00 - 2 * n1 * total * b10 + total ** 2 * b10) / denom
    variance_cr1 = total / (total - 2) * variance_cr0
    return dict(activity_mean=mean1, control_mean=mean0, difference=mean1 - mean0,
                se=math.sqrt(max(variance_cr1, 0.0)), df=total - 2,
                activity_clusters=n1, control_clusters=n0)


def confidence_interval(estimate, se, df, alpha):
    """给定显著性水平 alpha 的双侧 t 区间。"""
    critical = t_quantile(1.0 - alpha / 2.0, df)
    return [estimate - critical * se, estimate + critical * se]


def naive_slot_se(slots):
    """槽位层面的 Welch 标准误，仅作敏感性对照，不作正式推断。"""
    activity = [value for arm, value in slots if arm == 1]
    control = [value for arm, value in slots if arm == 0]
    if len(activity) < 2 or len(control) < 2:
        return None
    m1, m0 = sum(activity) / len(activity), sum(control) / len(control)
    v1 = sum((value - m1) ** 2 for value in activity) / (len(activity) - 1)
    v0 = sum((value - m0) ** 2 for value in control) / (len(control) - 1)
    return dict(difference=m1 - m0, se=math.sqrt(v1 / len(activity) + v0 / len(control)))
