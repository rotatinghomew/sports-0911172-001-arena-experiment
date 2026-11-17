from .store import require, money

class Assignment:
    def __init__(self, store):
        self.store = store

    def schedule(self, id, venue, weekend, arm, people):
        require(arm in ('activity','control'), '分组无效')
        require(len(people) == len(set(people)), '机会名单重复')
        return self.store.put('opportunity', id, dict(venue=venue, weekend=weekend,
            arm=arm, people=people, exposures=[], schedule_revision=0), create=True)

    def expose(self, id, person, actual_venue, happened_at):
        with self.store.atomic():
            record = self.store.get('opportunity', id)
            require(person in record['people'], '不在该次安排中')
            require(not any(item['person']==person for item in record['exposures']), '重复曝光登记')
            record['exposures'].append(dict(person=person, venue=actual_venue, happened_at=happened_at))
            return self.store.put('opportunity', id, record)

    def revise(self, id, people, reason):
        require(bool(reason), '修订应说明原因')
        require(len(people)==len(set(people)), '机会名单重复')
        record = self.store.get('opportunity', id)
        require(all(item['person'] in people for item in record['exposures']), '已曝光机会不能删除')
        record.update(people=people, reason=reason,
                      schedule_revision=record.get('schedule_revision', 0) + 1)
        return self.store.put('opportunity', id, record)

    def totals(self):
        result = {}
        for record in self.store.all('opportunity'):
            group = result.setdefault(record['arm'], dict(assigned=0, exposed=0))
            group['assigned'] += len(record['people'])
            group['exposed'] += len(record['exposures'])
        return result
