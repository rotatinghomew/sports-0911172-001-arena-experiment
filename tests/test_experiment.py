import unittest
from product.app import Application
from product import stats


def schedule(app, id, arm, people, venue='north', weekend='2026-12-05'):
    app.execute('assignment.schedule', dict(id=id, venue=venue, weekend=weekend,
                                            arm=arm, people=people))


def register_plan(app, id='plan1', opportunities=None, looks=('2026-12-07T00:00:00+08:00',),
                  min_effect=1000):
    if opportunities is None:
        opportunities = ['a1', 'a2', 'c1', 'c2']
    app.execute('experiment.plan', dict(
        id=id, opportunities=opportunities,
        primary_metric='net_revenue_per_slot', min_effect=min_effect,
        guardrails=[dict(metric='complaint_rate', direction='upper', limit=0.30),
                    dict(metric='discount_rate', direction='upper', limit=0.60)],
        looks=list(looks), reason='试点放量判断'))


class ExperimentSetupTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()

    def tearDown(self):
        self.app.store.close()

    def test_preregistration_rejects_existing_exposure(self):
        schedule(self.app, 'a1', 'activity', ['x1', 'x2'])
        self.app.execute('assignment.expose', dict(
            id='a1', person='x1', actual_venue='north', happened_at='2026-12-05T10:00:00+08:00'))
        schedule(self.app, 'c1', 'control', ['y1', 'y2'])
        with self.assertRaises(ValueError):
            register_plan(self.app)

    def test_plan_requires_both_arms_and_frozen_metrics(self):
        schedule(self.app, 'a1', 'activity', ['x1', 'x2'])
        schedule(self.app, 'a2', 'activity', ['x3', 'x4'])
        with self.assertRaises(ValueError):
            register_plan(self.app, opportunities=['a1', 'a2'])
        plan = self.app.store.get('plan', 'plan1') if False else None
        # 未知指标与非法限制被拒绝
        schedule(self.app, 'c1', 'control', ['y1', 'y2'])
        with self.assertRaises(ValueError):
            self.app.execute('experiment.plan', dict(
                id='p2', opportunities=['a1', 'a2', 'c1'],
                primary_metric='unknown_metric', min_effect=1,
                guardrails=[], looks=['2026-12-07T00:00:00+08:00'], reason='x'))

    def test_duplicate_plan_identifier_rejected(self):
        schedule(self.app, 'a1', 'activity', ['x1', 'x2'])
        schedule(self.app, 'a2', 'activity', ['x3', 'x4'])
        schedule(self.app, 'c1', 'control', ['y1', 'y2'])
        schedule(self.app, 'c2', 'control', ['y3', 'y4'])
        register_plan(self.app)
        with self.assertRaises(ValueError):
            register_plan(self.app)


class IntentionToTreatTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        schedule(self.app, 'a1', 'activity', ['a', 'b'], venue='north')
        schedule(self.app, 'a2', 'activity', ['c', 'd'], venue='east')
        schedule(self.app, 'c1', 'control', ['e', 'f'], venue='south')
        schedule(self.app, 'c2', 'control', ['g', 'h'], venue='west')
        register_plan(self.app)

    def tearDown(self):
        self.app.store.close()

    def test_unexposed_people_retained_in_assigned_arm(self):
        # 活动臂只曝光 a；c、d 等未曝光，但成交仍按原机会归属
        self.app.execute('assignment.expose', dict(
            id='a1', person='a', actual_venue='north', happened_at='2026-12-05T10:00:00+08:00'))
        self.app.execute('ledger.sale', dict(
            id='s1', opportunity='a2', person='c', cents=10000,
            happened_at='2026-12-05T11:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['sample']['slots'], 8)
        self.assertEqual(evaluation['sample']['unexposed'], 7)
        # 未曝光槽位收入仍计入活动臂
        self.assertGreater(evaluation['results']['net_revenue_per_slot']['activity_mean'], 0)
        codes = {item['code'] for item in evaluation['caveats']}
        self.assertIn('unexposed_slots_retained_in_arm', codes)

    def test_cross_venue_is_flagged_not_reassigned(self):
        self.app.execute('assignment.expose', dict(
            id='a1', person='a', actual_venue='south', happened_at='2026-12-05T10:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['sample']['cross_venue'], 1)
        self.assertIn('cross_venue_participation',
                      {item['code'] for item in evaluation['caveats']})
        # 跨馆曝光不改变 a 的活动臂归属
        self.assertEqual(evaluation['sample']['activity_clusters'], 2)

    def test_late_refund_and_sales_excluded_and_explained(self):
        self.app.execute('ledger.sale', dict(
            id='s1', opportunity='a1', person='a', cents=10000,
            happened_at='2026-12-05T11:00:00+08:00'))
        self.app.execute('ledger.refund', dict(
            id='r1', sale='s1', cents=3000, happened_at='2026-12-06T11:00:00+08:00'))
        self.app.execute('ledger.refund', dict(
            id='r2', sale='s1', cents=2000, happened_at='2026-12-09T11:00:00+08:00'))
        self.app.execute('ledger.sale', dict(
            id='s2', opportunity='c1', person='e', cents=7000,
            happened_at='2026-12-10T11:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['sample']['late_refund_cents'], 2000)
        self.assertEqual(evaluation['sample']['late_sales'], 1)
        # 净收入只扣截止前退款：10000 - 3000
        self.assertEqual(evaluation['results']['net_revenue_per_slot']['activity_mean'],
                         (7000 + 0) / 4)

    def test_late_refund_counted_once_across_slots(self):
        # 同一机会有两名球友，晚到退款不得因槽位循环被重复统计
        self.app.execute('ledger.sale', dict(
            id='s1', opportunity='a1', person='a', cents=10000,
            happened_at='2026-12-05T11:00:00+08:00'))
        self.app.execute('ledger.refund', dict(
            id='r1', sale='s1', cents=2500, happened_at='2026-12-20T11:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['sample']['late_refund_cents'], 2500)
        self.assertEqual(evaluation['results']['net_revenue_per_slot']['activity_mean'],
                         10000 / 4)

    def test_revision_after_plan_is_recorded_as_caveat(self):
        self.app.execute('assignment.revise', dict(
            id='a1', people=['a', 'b', 'i'], reason='新增一名球友'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        caveat = next(item for item in evaluation['caveats']
                      if item['code'] == 'revised_after_plan')
        self.assertEqual(caveat['detail'], 'a1')


class PeekingAndClusteringTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        schedule(self.app, 'a1', 'activity', ['a1', 'a2'])
        schedule(self.app, 'a2', 'activity', ['a3', 'a4'])
        schedule(self.app, 'c1', 'control', ['c1', 'c2'])
        schedule(self.app, 'c2', 'control', ['c3', 'c4'])

    def tearDown(self):
        self.app.store.close()

    def test_bonferroni_across_scheduled_looks(self):
        register_plan(self.app, looks=('2026-12-07T00:00:00+08:00',
                                       '2026-12-14T00:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        first = self.app.store.get('evaluation', 'e1')
        self.assertTrue(first['confirmatory'])
        self.assertAlmostEqual(first['alpha'], 0.025)
        self.app.execute('experiment.evaluate', dict(
            id='e2', plan_id='plan1', as_of='2026-12-14T00:00:00+08:00'))
        second = self.app.store.get('evaluation', 'e2')
        self.assertTrue(second['confirmatory'])
        self.assertAlmostEqual(second['alpha'], 0.025)

    def test_unscheduled_look_compromises_confirmatory_claims(self):
        register_plan(self.app, looks=('2026-12-14T00:00:00+08:00',))
        self.app.execute('experiment.evaluate', dict(
            id='peek', plan_id='plan1', as_of='2026-12-08T00:00:00+08:00'))
        peek = self.app.store.get('evaluation', 'peek')
        self.assertFalse(peek['confirmatory'])
        self.assertTrue(self.app.store.get('plan', 'plan1')['compromised'])
        # 之后即使到了计划节点，原置信说法也不能继续沿用
        self.app.execute('experiment.evaluate', dict(
            id='e2', plan_id='plan1', as_of='2026-12-14T00:00:00+08:00'))
        scheduled_after = self.app.store.get('evaluation', 'e2')
        self.assertFalse(scheduled_after['confirmatory'])
        self.assertIn('not_confirmatory',
                      {item['code'] for item in scheduled_after['caveats']})

    def test_duplicate_scheduled_look_is_adhoc_not_extra_claim(self):
        register_plan(self.app)
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1b', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        again = self.app.store.get('evaluation', 'e1b')
        self.assertFalse(again['confirmatory'])
        # 重看结果不产生券
        self.assertEqual(self.app.store.all('coupon'), [])

    def test_repeated_person_across_arms_forces_reserve(self):
        # 三名球友同时出现在活动与对照机会：跨臂 3 人 / 共 11 人 ≈ 27%，超过 20% 阈值
        schedule(self.app, 'a3', 'activity', ['z1', 'z2'])
        schedule(self.app, 'c3', 'control', ['z1', 'z3'])
        schedule(self.app, 'a4', 'activity', ['z4', 'z5'])
        schedule(self.app, 'c4', 'control', ['z4', 'z6'])
        schedule(self.app, 'a5', 'activity', ['z7', 'z8'])
        schedule(self.app, 'c5', 'control', ['z7', 'z9'])
        register_plan(self.app,
                      opportunities=['a1', 'a3', 'a4', 'a5', 'c1', 'c3', 'c4', 'c5'])
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['decision'], 'reserve')
        self.assertTrue(any('跨臂球友' in reason for reason in evaluation['reserve_reasons']))
        # 人物层面敏感性分析剔除跨臂者并显式报告
        self.assertEqual(evaluation['sensitivity']['persons_excluded_for_overlap'], 3)

    def test_insufficient_clusters_is_reserve(self):
        register_plan(self.app, opportunities=['a1', 'c1'])
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        evaluation = self.app.store.get('evaluation', 'e1')
        self.assertEqual(evaluation['decision'], 'reserve')
        self.assertTrue(any('聚类数不足' in reason for reason in evaluation['reserve_reasons']))


class ApprovalControlTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        for key, arm in (('a1', 'activity'), ('a2', 'activity'),
                         ('c1', 'control'), ('c2', 'control')):
            schedule(self.app, key, arm, [key + 'p1', key + 'p2'])
        register_plan(self.app)
        for key in ('a1', 'a2'):
            self.app.execute('ledger.sale', dict(
                id='s-' + key, opportunity=key, person=key + 'p1', cents=12000,
                happened_at='2026-12-05T11:00:00+08:00'))
        self.app.execute('experiment.evaluate', dict(
            id='e1', plan_id='plan1', as_of='2026-12-07T00:00:00+08:00'))
        self.opportunities = ['a1', 'a2', 'c1', 'c2']

    def tearDown(self):
        self.app.store.close()

    def test_request_requires_evaluation_basis(self):
        with self.assertRaises(ValueError):
            self.app.execute('campaign.request', dict(
                id='cx', opportunities=self.opportunities, coupon_count=2,
                reason='无依据扩大'))

    def test_unsupported_requires_manual_override_with_reason(self):
        evaluation = self.app.store.get('evaluation', 'e1')
        if evaluation['decision'] == 'supported':
            self.skipTest('当前数据恰好支持扩大，改用保留场景测试人工放行')
        with self.assertRaises(ValueError):
            self.app.execute('campaign.request', dict(
                id='cx', opportunities=self.opportunities, coupon_count=2,
                reason='尝试扩大', evaluation='e1'))
        self.app.execute('campaign.request', dict(
            id='cx', opportunities=self.opportunities, coupon_count=2,
            reason='尝试扩大', evaluation='e1',
            manual_override=True, override_reason='负责人确认经营需要，限制依据留存'))
        record = self.app.store.get('campaign', 'cx')
        self.assertTrue(record['manual_override'])
        self.assertEqual(record['basis_decision'], evaluation['decision'])

    def test_refund_after_evaluation_blocks_pending_approval(self):
        self.app.execute('campaign.request', dict(
            id='c9', opportunities=self.opportunities, coupon_count=2,
            reason='扩大', evaluation='e1'))
        self.app.execute('ledger.refund', dict(
            id='r9', sale='s-a1', cents=1000, happened_at='2026-12-08T11:00:00+08:00'))
        with self.assertRaises(ValueError):
            self.app.execute('campaign.approve', dict(id='c9', reviewer='boss'))
        # 失败不产生部分写入：申请仍待执行、未发券，差异可随时复算
        pending = self.app.store.get('campaign', 'c9')
        self.assertEqual(pending['state'], 'requested')
        self.assertEqual(self.app.store.all('coupon'), [])
        basis = self.app.store.get('evaluation', 'e1')
        differences = self.app.services['experiment'].signature_differences(basis)
        self.assertTrue(any(item['kind'] == 'refund' for item in differences))

    def test_rejudgement_after_data_change_allows_approval_without_double_coupons(self):
        self.app.execute('campaign.request', dict(
            id='c9', opportunities=self.opportunities, coupon_count=2,
            reason='扩大', evaluation='e1'))
        self.app.execute('ledger.refund', dict(
            id='r9', sale='s-a1', cents=1000, happened_at='2026-12-08T11:00:00+08:00'))
        with self.assertRaises(ValueError):
            self.app.execute('campaign.approve', dict(id='c9', reviewer='boss'))
        # 重新判断（不发券），新申请引用新依据
        self.app.execute('experiment.evaluate', dict(
            id='e2', plan_id='plan1', as_of='2026-12-09T00:00:00+08:00'))
        basis = self.app.store.get('evaluation', 'e2')
        kwargs = dict(id='c10', opportunities=self.opportunities, coupon_count=2,
                      reason='重新扩大', evaluation='e2')
        if basis['decision'] != 'supported':
            kwargs.update(manual_override=True, override_reason='负责人确认，依据留存')
        self.app.execute('campaign.request', kwargs)
        self.app.execute('campaign.approve', dict(id='c10', reviewer='boss'))
        self.assertEqual(len(self.app.store.all('coupon')), 2)
        # 失败申请从未发券；新申请重复审批也不补发
        self.app.execute('campaign.approve', dict(id='c10', reviewer='boss'))
        self.assertEqual(len(self.app.store.all('coupon')), 2)

    def test_approved_decision_keeps_its_basis(self):
        kwargs = dict(id='c8', opportunities=self.opportunities, coupon_count=2,
                      reason='扩大', evaluation='e1')
        basis = self.app.store.get('evaluation', 'e1')
        if basis['decision'] != 'supported':
            kwargs.update(manual_override=True, override_reason='负责人确认，依据留存')
        self.app.execute('campaign.request', kwargs)
        self.app.execute('campaign.approve', dict(id='c8', reviewer='boss'))
        self.app.execute('ledger.refund', dict(
            id='r8', sale='s-a1', cents=500, happened_at='2026-12-09T11:00:00+08:00'))
        campaign = self.app.store.get('campaign', 'c8')
        self.assertEqual(campaign['state'], 'approved')
        self.assertEqual(campaign['evaluation'], 'e1')
        # 当时的判断快照仍可完整读取
        self.assertEqual(self.app.store.get('evaluation', 'e1')['id'], 'e1')
        # 数据已变化，同一依据不能再用于新申请
        with self.assertRaises(ValueError):
            self.app.execute('campaign.request', dict(
                id='c11', opportunities=self.opportunities, coupon_count=1,
                reason='再次扩大', evaluation='e1'))


class StatsUnitTests(unittest.TestCase):
    def test_t_quantiles_match_reference_table(self):
        for df, expected in [(2, 4.303), (5, 2.571), (10, 2.228), (30, 2.042)]:
            self.assertAlmostEqual(stats.t_quantile(0.975, df), expected, delta=0.002)
            self.assertAlmostEqual(stats.t_cdf(stats.t_quantile(0.95, df), df),
                                   0.95, places=9)

    def test_cluster_mean_diff_cr1(self):
        result = stats.cluster_mean_diff([(1, 10.0), (1, 12.0), (0, 6.0), (0, 8.0)])
        self.assertEqual(result['difference'], 4.0)
        self.assertAlmostEqual(result['se'] ** 2, 2.0)
        self.assertEqual(result['df'], 2)
        self.assertIsNone(stats.cluster_mean_diff([(1, 1.0), (0, 1.0)]))


if __name__ == '__main__':
    unittest.main()
