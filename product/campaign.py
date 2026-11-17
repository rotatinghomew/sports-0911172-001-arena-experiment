from .store import require
from .experiment import Experiment


class Campaign:
    """放量（扩大）申请与审批。

    扩大申请必须引用对应活动的一次有效判断；负责人可以人工放行被判断为不支持或保留的
    申请，但必须写明原因，且限制依据（guardrail 结果与数据签名）一并留存，不能跳过。
    审批时重新核对判断固化的数据签名：安排修订、补登记曝光或新增退款都会使签名失配，
    待执行申请必须重判后才能发券。重复审批、审批失败后的重试不额外发券。
    """

    def __init__(self, store):
        self.store = store
        self.experiment = Experiment(store)

    def request(self, id, opportunities, coupon_count, reason, evaluation=None,
                manual_override=False, override_reason=''):
        require(coupon_count > 0 and bool(reason), '申请须包含数量和业务原因')
        require(len(opportunities) == len(set(opportunities)), '申请机会重复')
        for opportunity in opportunities:
            self.store.get('opportunity', opportunity)
        require(evaluation is not None, '扩大申请须引用一次实验判断')
        basis = self.store.get('evaluation', evaluation)
        plan = self.store.get('plan', basis['plan_id'])
        require(set(plan['opportunities']) == set(opportunities),
                '申请机会须与判断对应的活动范围一致')
        require(basis['decision'] in ('supported', 'not_supported', 'reserve'), '判断状态无效')
        if basis['decision'] != 'supported':
            require(manual_override and bool(override_reason),
                    '判断未支持扩大时须人工放行并写明原因')
        else:
            manual_override = False
            override_reason = ''
        matches, _ = self.experiment.signature_matches(basis)
        require(matches, '判断依据已失效（安排或售后数据发生变化），请重新判断后再申请')
        return self.store.put('campaign', id, dict(
            opportunities=opportunities, coupon_count=coupon_count, reason=reason,
            evaluation=evaluation, manual_override=manual_override,
            override_reason=override_reason,
            basis_decision=basis['decision'], state='requested'), create=True)

    def approve(self, id, reviewer):
        with self.store.atomic():
            record = self.store.get('campaign', id)
            if record['state'] == 'approved':
                require(record['reviewer'] == reviewer, '已由其他人审批')
                return record
            require(record['state'] == 'requested' and bool(reviewer), '审批状态无效')
            basis = self.store.get('evaluation', record['evaluation'])
            matches, _ = self.experiment.signature_matches(basis)
            if not matches:
                # 不做部分写入：事务回滚后申请保持待执行状态，差异可通过
                # experiment.signature_differences 随时复算；重判后重新申请即可
                raise ValueError('判断依据已失效，审批阻断：请重新判断后重新申请')
            for index in range(record['coupon_count']):
                self.store.put('coupon', f'{id}-{index}',
                               dict(campaign=id, state='issued'), create=True)
            record.update(state='approved', reviewer=reviewer)
            return self.store.put('campaign', id, record)

    def cancel(self, id):
        record = self.store.get('campaign', id)
        require(record['state'] == 'requested', '只能撤回待审批申请')
        record['state'] = 'cancelled'
        return self.store.put('campaign', id, record)
