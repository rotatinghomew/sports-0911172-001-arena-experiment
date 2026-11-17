from .store import require, money

class Campaign:
    def __init__(self, store):
        self.store = store

    def request(self, id, opportunities, coupon_count, reason):
        require(coupon_count>0 and bool(reason), '申请须包含数量和业务原因')
        for opportunity in opportunities:
            self.store.get('opportunity',opportunity)
        return self.store.put('campaign', id, dict(opportunities=opportunities,
            coupon_count=coupon_count,reason=reason,state='requested'), create=True)

    def approve(self, id, reviewer):
        with self.store.atomic():
            record = self.store.get('campaign',id)
            if record['state']=='approved':
                require(record['reviewer']==reviewer, '已由其他人审批')
                return record
            require(record['state']=='requested' and bool(reviewer), '审批状态无效')
            for index in range(record['coupon_count']):
                self.store.put('coupon',f'{id}-{index}',dict(campaign=id,state='issued'),create=True)
            record.update(state='approved',reviewer=reviewer)
            return self.store.put('campaign',id,record)

    def cancel(self, id):
        record = self.store.get('campaign',id)
        require(record['state']=='requested','只能撤回待审批申请')
        record['state']='cancelled'
        return self.store.put('campaign',id,record)
