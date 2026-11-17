"""实验分析与放量控制服务。

设计原则对应业务约定：
- 以预先安排的活动机会为随机化单位做 ITT：未曝光者保留在原臂，核销者子集不单独推断；
- 同机会（同场同行者）为聚类做 CR1 稳健推断；同一球友跨机会重复出现只进入人物层面
  敏感性分析，不作为独立证据；
- 实际未曝光、跨馆参加、as-of 之后的退款/成交均显式登记为 caveat；无法辨认的干扰或
  证据不足时决策强制 reserve；
- 指标、限制与查看节点在计划中预先注册并冻结；计划外查看会使计划丧失确认性，之后的
  置信说法只能作为探索性参考；计划内多次查看按 Bonferroni 校正；
- 每次判断固化数据签名，审批时签名失配（安排修订、补登记曝光、退款等）即阻断发券，
  必须重新判断；判断与重试本身不产生任何券。
"""
import hashlib
import json
from datetime import datetime, timezone

from .store import require
from . import stats

METRICS = ('net_revenue_per_slot', 'complaint_rate', 'discount_rate')
RATE_METRICS = ('complaint_rate', 'discount_rate')
DEFAULT_ALPHA = 0.05
# 超过该比例的跨馆曝光视为存在无法辨认的馆间干扰
CROSS_VENUE_LIMIT = 0.20
# 超过该比例的球友同时出现在两臂，视为独立证据不可辨认
PERSON_OVERLAP_LIMIT = 0.20
# 每臂最少机会（聚类）数，不足则证据不足
MIN_CLUSTERS_PER_ARM = 2


def parse_time(value):
    """零补齐日期按 UTC 零点处理，带时区时间字符串保留原时区。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def metric_value(metric, slot):
    if metric == 'net_revenue_per_slot':
        return float(slot['net'])
    if metric == 'complaint_rate':
        return float(slot['complaint'])
    if metric == 'discount_rate':
        return float(slot['discount'])
    raise ValueError('未知指标')


class Experiment:
    def __init__(self, store):
        self.store = store

    # ---------- 预先注册 ----------

    def plan(self, id, opportunities, primary_metric, min_effect, guardrails, looks, reason):
        require(bool(reason), '注册计划须说明业务原因')
        require(primary_metric in METRICS, '主要收益指标无效')
        require(len(opportunities) == len(set(opportunities)), '机会名单重复')
        require(len(looks) == len(set(looks)) and looks, '查看节点无效')
        parsed_looks = sorted(parse_time(value) for value in looks)
        if primary_metric in RATE_METRICS:
            require(0.0 <= float(min_effect) <= 1.0, '比率指标的最低效果须在 [0,1]')
        else:
            require(isinstance(min_effect, int) and min_effect >= 0, '最低效果须为非负整数分')
        normalized_guardrails = []
        for item in guardrails:
            metric = item['metric']
            require(metric in METRICS, '限制指标无效')
            require(item.get('direction') == 'upper', '目前仅支持上限型经营限制')
            limit = float(item['limit'])
            if metric in RATE_METRICS:
                require(0.0 <= limit <= 1.0, '比率限制须在 [0,1]')
            normalized_guardrails.append(dict(metric=metric, direction='upper', limit=limit))
        arms = set()
        for opportunity in opportunities:
            record = self.store.get('opportunity', opportunity)
            arms.add(record['arm'])
            require(not record['exposures'], '机会已产生曝光登记，不能再预先注册')
        require(arms == {'activity', 'control'}, '计划须同时包含活动与对照机会')
        require(not self._scope_has_sales(opportunities), '机会已产生成交，不能再预先注册')
        return self.store.put('plan', id, dict(
            opportunities=list(opportunities), primary_metric=primary_metric,
            min_effect=min_effect, guardrails=normalized_guardrails,
            # 统一为带时区 ISO 串，使 evaluate 传入原始日期或 ISO 串都能匹配同一节点
            looks=[value.astimezone(timezone.utc).isoformat()
                   for value in parsed_looks], reason=reason,
            state='open', compromised=False, closed_early=False,
            scope_revisions=self._revision_snapshot(opportunities)), create=True)

    def close(self, plan_id, reason):
        require(bool(reason), '关闭计划须说明原因')
        record = self.store.get('plan', plan_id)
        require(record['state'] == 'open', '计划已关闭')
        remaining = [value for value in record['looks']
                     if not self._evaluation_exists(plan_id, value)]
        record.update(state='closed', closed_early=bool(remaining), close_reason=reason)
        return self.store.put('plan', plan_id, record)

    # ---------- 判断 ----------

    def evaluate(self, id, plan_id, as_of, note=''):
        plan = self.store.get('plan', plan_id)
        require(plan['state'] == 'open', '计划已关闭，不能新增判断')
        cutoff = parse_time(as_of)
        normalized_looks = [parse_time(value) for value in plan['looks']]
        is_look = any(value == cutoff for value in normalized_looks)
        scheduled = is_look and not self._evaluation_exists(plan_id, as_of)
        if not scheduled and not plan['compromised']:
            # 计划外查看：原确认性说法即刻失效，并污染其后所有判断
            plan['compromised'] = True
            self.store.put('plan', plan_id, plan)
            plan = self.store.get('plan', plan_id)
        confirmatory = scheduled and not plan['compromised']
        look_index = next((index + 1 for index, value in enumerate(normalized_looks)
                           if value == cutoff), None)
        alpha = DEFAULT_ALPHA / len(plan['looks']) if confirmatory else DEFAULT_ALPHA

        dataset = self._build_dataset(plan, cutoff)
        needed_metrics = [plan['primary_metric']] + [
            item['metric'] for item in plan['guardrails']]
        results, reserve_reasons, caveats = {}, [], list(dataset['caveats'])
        for metric in dict.fromkeys(needed_metrics):
            clusters = [(1 if record['arm'] == 'activity' else 0,
                         sum(metric_value(metric, slot) for slot in record['slots'])
                         / len(record['slots']))
                        for record in dataset['opportunities']]
            outcome = stats.cluster_mean_diff(clusters)
            if outcome is None:
                reserve_reasons.append(f'指标 {metric} 的每臂聚类数不足 {MIN_CLUSTERS_PER_ARM}')
                results[metric] = None
                continue
            ci = stats.confidence_interval(outcome['difference'], outcome['se'],
                                           outcome['df'], alpha)
            results[metric] = dict(outcome, alpha=alpha,
                                   confidence_interval=[ci[0], ci[1]])

        criteria, decision = self._decide(plan, results, dataset, confirmatory,
                                          reserve_reasons, caveats)
        signature = self._signature(plan['opportunities'])
        payload = dict(plan_id=plan_id, as_of=as_of, scheduled=scheduled,
                       look_index=look_index, confirmatory=confirmatory, alpha=alpha,
                       note=note, sample=dataset['sample'], results=results,
                       sensitivity=self._sensitivity(dataset, needed_metrics),
                       caveats=caveats, reserve_reasons=reserve_reasons,
                       criteria=criteria, decision=decision, signature=signature)
        return self.store.put('evaluation', id, payload, create=True)

    def _decide(self, plan, results, dataset, confirmatory, reserve_reasons, caveats):
        sample = dataset['sample']
        if sample['cross_venue_rate'] > CROSS_VENUE_LIMIT:
            reserve_reasons.append(
                f'跨馆曝光比例 {sample["cross_venue_rate"]:.3f} 超过 {CROSS_VENUE_LIMIT}，'
                '馆间干扰无法辨认')
            caveats.append(dict(code='cross_venue_over_limit',
                                detail=sample['cross_venue_rate']))
        if sample['person_overlap_rate'] > PERSON_OVERLAP_LIMIT:
            reserve_reasons.append(
                f'跨臂球友比例 {sample["person_overlap_rate"]:.3f} 超过 {PERSON_OVERLAP_LIMIT}，'
                '重复球友不能作为独立证据')
            caveats.append(dict(code='person_overlap_over_limit',
                                detail=sample['person_overlap_rate']))
        if not confirmatory:
            reserve_reasons.append('该判断不具确认性（计划外查看或计划已被污染），'
                                   '置信区间仅为探索性参考')
            caveats.append(dict(code='not_confirmatory', detail=None))

        criteria = []
        primary = plan['primary_metric']
        if results[primary] is not None:
            bound = results[primary]['confidence_interval'][0]
            met = bound > plan['min_effect']
            criteria.append(dict(kind='benefit', metric=primary,
                                 min_effect=plan['min_effect'], lower_bound=bound, met=met))
        for guardrail in plan['guardrails']:
            metric = guardrail['metric']
            if results[metric] is None:
                continue
            bound = results[metric]['confidence_interval'][1]
            met = bound < guardrail['limit']
            criteria.append(dict(kind='guardrail', metric=metric, limit=guardrail['limit'],
                                 upper_bound=bound, met=met))
        if reserve_reasons or any(value is None for value in results.values()):
            return criteria, 'reserve'
        if all(item['met'] for item in criteria):
            return criteria, 'supported'
        return criteria, 'not_supported'

    def _sensitivity(self, dataset, needed_metrics):
        slots = dataset['slots']
        naive = {}
        for metric in dict.fromkeys(needed_metrics):
            naive[metric] = stats.naive_slot_se(
                [(1 if slot['arm'] == 'activity' else 0, metric_value(metric, slot))
                 for slot in slots])
        # 人物层面聚类：仅使用从未跨臂的球友；跨臂者计入 overlap caveat
        persons = {}
        for slot in slots:
            persons.setdefault(slot['person'], []).append(slot)
        person_clusters, overlap = [], 0
        for person, records in persons.items():
            arms = {record['arm'] for record in records}
            if len(arms) > 1:
                overlap += 1
                continue
            person_clusters.append((1 if 'activity' in arms else 0, person))
        person_block = {}
        for metric in dict.fromkeys(needed_metrics):
            values = []
            for arm, person in person_clusters:
                records = persons[person]
                values.append((arm, sum(metric_value(metric, slot) for slot in records)
                               / len(records)))
            person_block[metric] = stats.cluster_mean_diff(values)
        return dict(naive_slot=naive, person_clustered=person_block,
                    persons_total=len(persons), persons_excluded_for_overlap=overlap)

    # ---------- 数据组装（as-of ITT） ----------

    def _build_dataset(self, plan, cutoff):
        opportunities, slots, caveats = [], [], []
        exposed = unexposed = cross_venue = 0
        late_refund_cents = late_sales = 0
        sales_by_opportunity = {}
        for sale in self.store.all('sale'):
            sales_by_opportunity.setdefault(sale['opportunity'], []).append(sale)
        refunds_by_sale = {}
        for refund in self.store.all('refund'):
            refunds_by_sale.setdefault(refund['sale'], []).append(refund)
        counted_late_refunds = set()
        for opportunity_id in plan['opportunities']:
            record = self.store.get('opportunity', opportunity_id)
            exposures = {item['person']: item for item in record['exposures']
                         if parse_time(item['happened_at']) <= cutoff}
            opportunity_slots = []
            for person in record['people']:
                exposure = exposures.get(person)
                if exposure is None:
                    unexposed += 1
                else:
                    exposed += 1
                    if exposure['venue'] != record['venue']:
                        cross_venue += 1
                net = complaint = discount = 0
                for sale in sales_by_opportunity.get(opportunity_id, []):
                    if sale['person'] != person:
                        continue
                    if parse_time(sale['happened_at']) > cutoff:
                        late_sales += 1
                        continue
                    refunded = 0
                    for refund in refunds_by_sale.get(sale['id'], ()):
                        if parse_time(refund['happened_at']) <= cutoff:
                            refunded += refund['cents']
                        elif refund['id'] not in counted_late_refunds:
                            counted_late_refunds.add(refund['id'])
                            late_refund_cents += refund['cents']
                    net += sale['cents'] - refunded
                    complaint = max(complaint, int(sale['complaint']))
                    discount = max(discount, int(not sale['full_price']))
                slot = dict(opportunity=opportunity_id, arm=record['arm'], person=person,
                            net=net, complaint=complaint, discount=discount,
                            exposed=int(exposure is not None),
                            cross_venue=int(exposure is not None
                                            and exposure['venue'] != record['venue']))
                opportunity_slots.append(slot)
                slots.append(slot)
            opportunities.append(dict(arm=record['arm'], slots=opportunity_slots))
            planned_revision = plan['scope_revisions'].get(opportunity_id)
            if planned_revision is not None and record.get(
                    'schedule_revision', 0) != planned_revision:
                caveats.append(dict(code='revised_after_plan', detail=opportunity_id))
        total_slots = len(slots)
        arm_of_person = {}
        for slot in slots:
            arm_of_person.setdefault(slot['person'], set()).add(slot['arm'])
        overlap_persons = sum(1 for arms in arm_of_person.values() if len(arms) > 1)
        sample = dict(
            opportunities=len(opportunities), slots=total_slots,
            activity_clusters=sum(1 for item in opportunities if item['arm'] == 'activity'),
            control_clusters=sum(1 for item in opportunities if item['arm'] == 'control'),
            exposed=exposed, unexposed=unexposed,
            exposure_rate=exposed / total_slots if total_slots else 0.0,
            cross_venue=cross_venue,
            cross_venue_rate=cross_venue / exposed if exposed else 0.0,
            distinct_persons=len(arm_of_person), overlap_persons=overlap_persons,
            person_overlap_rate=overlap_persons / len(arm_of_person) if arm_of_person else 0.0,
            late_refund_cents=late_refund_cents, late_sales=late_sales)
        caveats.append(dict(code='unexposed_slots_retained_in_arm',
                            detail=dict(count=unexposed, exposure_rate=sample['exposure_rate'])))
        if cross_venue:
            caveats.append(dict(code='cross_venue_participation',
                                detail=dict(count=cross_venue,
                                            rate=sample['cross_venue_rate'])))
        if late_refund_cents:
            caveats.append(dict(code='refunds_after_cutoff_excluded',
                                detail=dict(cents=late_refund_cents)))
        if late_sales:
            caveats.append(dict(code='sales_after_cutoff_excluded',
                                detail=dict(count=late_sales)))
        return dict(opportunities=opportunities, slots=slots,
                    caveats=caveats, sample=sample)

    # ---------- 数据签名与审批依据 ----------

    def _revision_snapshot(self, opportunities):
        return {opportunity: self.store.get('opportunity', opportunity).get(
            'schedule_revision', 0)
                for opportunity in opportunities}

    def _signature(self, opportunities):
        scope = set(opportunities)
        opportunity_of_sale = {sale['id']: sale['opportunity']
                               for sale in self.store.all('sale')}
        parts = {
            'opportunity': sorted((item['id'], item['revision'])
                                  for item in self.store.all('opportunity')
                                  if item['id'] in scope),
            'sale': sorted((item['id'], item['revision'])
                           for item in self.store.all('sale')
                           if item['opportunity'] in scope),
            'refund': sorted((item['id'], item['revision'])
                             for item in self.store.all('refund')
                             if opportunity_of_sale.get(item['sale']) in scope),
        }
        encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        return dict(hash=hashlib.sha256(encoded).hexdigest(), parts=parts)

    def signature_matches(self, evaluation):
        current = self._signature(self.store.get('plan', evaluation['plan_id'])['opportunities'])
        return current['hash'] == evaluation['signature']['hash'], current

    def signature_differences(self, evaluation):
        matches, current = self.signature_matches(evaluation)
        if matches:
            return []
        before = evaluation['signature'].get('parts', {})
        differences = []
        for kind in ('opportunity', 'sale', 'refund'):
            old_parts = dict(before.get(kind, ()))
            new_parts = dict(current['parts'].get(kind, ()))
            for key in sorted(set(old_parts) | set(new_parts)):
                if old_parts.get(key) != new_parts.get(key):
                    differences.append(dict(kind=kind, key=key,
                                            before=old_parts.get(key), after=new_parts.get(key)))
        return differences

    # ---------- 辅助查询 ----------

    def _scope_has_sales(self, opportunities):
        scope = set(opportunities)
        return any(sale['opportunity'] in scope for sale in self.store.all('sale'))

    def _evaluation_exists(self, plan_id, as_of):
        cutoff = parse_time(as_of)
        return any(item['plan_id'] == plan_id
                   and parse_time(item['as_of']) == cutoff
                   for item in self.store.all('evaluation'))
