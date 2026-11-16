"""展区完成率监测规则引擎测试：纯函数，不依赖数据库/Redis。

覆盖需求口径：
- 固定基准与下降门槛；缺报不视作正常且计入连续异常
- 连续下降达到门槛才开告警，恢复后解除，再次下降可重新告警
- 更正后重算，失去依据的告警撤销；确认意见由上层持久化保留
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.services.monitoring import evaluate_window, build_episodes, DailyStatus

MON = {'start_date': '2026-11-01', 'baseline_rate': 50.0, 'drop_threshold': 10.0, 'consecutive_limit': 3}
# 下限 = 40%

def flow(ds, arm, visitors=100, completed=50):
    return {'log_date': ds, 'arm': arm, 'visitors': visitors, 'completed': completed}

def both(ds, rate_pct, visitors=100):
    c = round(visitors * rate_pct / 100)
    return [flow(ds, 'A', visitors, c), flow(ds, 'B', visitors, c)]

def test_all_normal_no_episode():
    flows = sum((both(f'2026-11-{d:02d}', 55) for d in range(1, 6)), [])
    rows = evaluate_window(MON, flows, '2026-11-05')
    assert all(r['status'] == DailyStatus.normal.value and r['streak'] == 0 for r in rows)
    assert build_episodes(rows, 3) == []

def test_below_threshold_boundary_and_streak():
    # 45% 不低于 40% 下限 → 正常；39% → 下降
    flows = sum((both(f'2026-11-{d:02d}', 45 if d == 1 else 39) for d in range(1, 5)), [])
    rows = evaluate_window(MON, flows, '2026-11-04')
    assert rows[0]['status'] == 'normal'
    assert [r['streak'] for r in rows[1:]] == [1, 2, 3]
    eps = build_episodes(rows, 3)
    assert len(eps) == 1 and eps[0]['streak_start'] == '2026-11-02' and eps[0]['streak_end'] == '2026-11-04'
    assert eps[0]['resolved'] is False

def test_missing_counts_as_abnormal_but_not_normal():
    flows = sum((both(f'2026-11-{d:02d}', 39) for d in range(1, 3)), [])  # 1、2 日下降
    flows += [flow('2026-11-03', 'A')]                                     # 3 日仅 A，缺报
    rows = evaluate_window(MON, flows, '2026-11-04')
    assert rows[2]['status'] == 'missing' and rows[2]['rate'] is None and rows[2]['streak'] == 3
    assert rows[3]['status'] == 'missing' and rows[3]['streak'] == 4       # 完全没数据也是缺报
    eps = build_episodes(rows, 3)
    assert len(eps) == 1 and '缺报' in eps[0]['kinds']

def test_recovery_resets_streak_and_resolves_episode():
    flows = sum((both(f'2026-11-{d:02d}', 39) for d in range(1, 4)), [])  # 连续 3 日下降
    flows += both('2026-11-04', 60)                                        # 恢复
    rows = evaluate_window(MON, flows, '2026-11-04')
    assert rows[3]['status'] == 'normal' and rows[3]['streak'] == 0
    eps = build_episodes(rows, 3)
    assert eps[0]['resolved'] is True and eps[0]['resolve_date'] == '2026-11-04'

def test_re_alert_after_recovery_is_new_episode():
    flows = sum((both(f'2026-11-{d:02d}', 39) for d in range(1, 4)), [])
    flows += both('2026-11-04', 60)
    flows += sum((both(f'2026-11-{d:02d}', 35) for d in range(5, 8)), [])  # 再次连续三日下降
    rows = evaluate_window(MON, flows, '2026-11-07')
    eps = build_episodes(rows, 3)
    assert [e['streak_start'] for e in eps] == ['2026-11-01', '2026-11-05']
    assert eps[0]['resolved'] is True and eps[1]['resolved'] is False

def test_short_dip_below_limit_does_not_alert():
    flows = both('2026-11-01', 60) + both('2026-11-02', 39) + both('2026-11-03', 39) + both('2026-11-04', 60)
    rows = evaluate_window(MON, flows, '2026-11-04')
    assert build_episodes(rows, 3) == []  # 只连续 2 日，不到门槛

def test_combined_rate_uses_weighted_arms():
    # A 高客流低完成率、B 低客流高完成率：合计按人数加权而非平均
    flows = [flow('2026-11-01', 'A', 100, 30), flow('2026-11-01', 'B', 10, 9)]  # 合计 39/110=35.45%
    rows = evaluate_window(MON, flows, '2026-11-01')
    assert rows[0]['status'] == 'below'
    assert abs(rows[0]['rate'] - 35.4545) < 0.01

def test_correction_breaks_episode_then_recomputed_window_has_no_alert():
    # 原始：1-3 日连续下降；更正 2 日回到正常后，异常区间断裂为两段单日，均不足门槛
    original = sum((both(f'2026-11-{d:02d}', 39) for d in range(1, 4)), [])
    rows_before = evaluate_window(MON, original, '2026-11-03')
    assert len(build_episodes(rows_before, 3)) == 1
    corrected = [f for f in original if f['log_date'] != '2026-11-02'] + both('2026-11-02', 55)
    rows_after = evaluate_window(MON, corrected, '2026-11-03')
    assert build_episodes(rows_after, 3) == []
    assert [r['status'] for r in rows_after] == ['below', 'normal', 'below']
    assert rows_after[2]['streak'] == 1  # 连续天数已随更正重算
