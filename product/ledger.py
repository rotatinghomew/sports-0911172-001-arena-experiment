from .store import require, money

class Ledger:
    def __init__(self, store):
        self.store = store

    def sale(self, id, opportunity, person, cents, happened_at, full_price=True, complaint=False):
        record = self.store.get('opportunity', opportunity)
        require(person in record['people'], '成交须关联已安排机会')
        return self.store.put('sale', id, dict(opportunity=opportunity, person=person,
            cents=money(cents), refunded=0, happened_at=happened_at,
            full_price=bool(full_price), complaint=bool(complaint)), create=True)

    def refund(self, id, sale, cents, happened_at):
        with self.store.atomic():
            record = self.store.get('sale', sale)
            require(record['refunded']+money(cents)<=record['cents'], '退款超过成交')
            refund = self.store.put('refund', id, dict(sale=sale,cents=cents,happened_at=happened_at), create=True)
            record['refunded'] += cents
            self.store.put('sale', sale, record)
            return refund

    def totals(self):
        groups = {}
        for sale in self.store.all('sale'):
            arm = self.store.get('opportunity',sale['opportunity'])['arm']
            groups[arm] = groups.get(arm,0)+sale['cents']-sale['refunded']
        return groups
