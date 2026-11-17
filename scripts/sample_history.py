import random
import json
from datetime import date, timedelta


def generate():
    rng = random.Random(117201)
    operations = []

    def add(command, **arguments):
        operations.append([command, arguments])

    def day(offset):
        return (date(2026, 7, 1) + timedelta(days=offset)).isoformat()

    keys = []
    for week in range(12):
        for venue in range(4):
            key = f'w{week}v{venue}'
            keys.append(key)
            people = [f'p{(index + venue * 9 + week * 2) % 80}' for index in range(24)]
            add('assignment.schedule', id=key, venue=f'v{venue}', weekend=day(week * 7),
                arm='activity' if (week + venue) % 2 == 0 else 'control', people=people)

    # 全部机会安排完成后再预先注册：指标、经营限制与查看节点在此冻结
    add('experiment.plan', id='hist-plan', opportunities=list(keys),
        primary_metric='net_revenue_per_slot', min_effect=200,
        guardrails=[
            dict(metric='complaint_rate', direction='upper', limit=0.20),
            dict(metric='discount_rate', direction='upper', limit=0.80)],
        looks=[day(12 * 7), day(12 * 7 + 7)],
        reason='历史合成样本的实验分析自检，不代表真实总体')

    for week in range(12):
        for venue in range(4):
            key = f'w{week}v{venue}'
            people = [f'p{(index + venue * 9 + week * 2) % 80}' for index in range(24)]
            for index, person in enumerate(people):
                if rng.random() < 0.65:
                    add('assignment.expose', id=key, person=person,
                        actual_venue=f'v{(venue + (index % 7 == 0)) % 4}',
                        happened_at=day(week * 7))
                if rng.random() < 0.5:
                    sale = f'{key}s{index}'
                    amount = rng.choice([4000, 6000, 8000, 12000])
                    add('ledger.sale', id=sale, opportunity=key, person=person,
                        cents=amount, happened_at=day(week * 7 + 1),
                        full_price=index % 4 != 0, complaint=rng.random() < 0.08)
                    if rng.random() < 0.15:
                        add('ledger.refund', id=f'{sale}r', sale=sale,
                            cents=amount // 2, happened_at=day(week * 7 + 5))

    # 计划内查看节点做判断；第二个节点的扩大申请在数据未再变化时可以正常审批
    add('experiment.evaluate', id='hist-eval-1', plan_id='hist-plan',
        as_of=day(12 * 7), note='第一个计划内查看节点')
    add('experiment.evaluate', id='hist-eval-2', plan_id='hist-plan',
        as_of=day(12 * 7 + 7), note='第二个计划内查看节点')
    add('campaign.request', id='hist-campaign', opportunities=list(keys),
        coupon_count=10, reason='历史样本放量流程自检', evaluation='hist-eval-2',
        manual_override=True,
        override_reason='合成数据不保证显著效果，用于演示人工放行时限制依据仍须留存')
    add('campaign.approve', id='hist-campaign', reviewer='operator')
    return operations


def main():
    import argparse
    parser = argparse.ArgumentParser(description='生成本地合成业务记录，不包含真实个人资料')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from pathlib import Path
    Path(args.output).write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
