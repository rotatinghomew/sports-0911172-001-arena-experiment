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
    for week in range(12):
        for venue in range(4):
            key = f'w{week}v{venue}'
            people = [f'p{(index + venue * 9 + week * 2) % 80}' for index in range(24)]
            add('assignment.schedule', id=key, venue=f'v{venue}', weekend=day(week * 7), arm='activity' if (week + venue) % 2 == 0 else 'control', people=people)
            for index, person in enumerate(people):
                if rng.random() < 0.65:
                    add('assignment.expose', id=key, person=person, actual_venue=f'v{(venue + (index % 7 == 0)) % 4}', happened_at=day(week * 7))
                if rng.random() < 0.5:
                    sale = f'{key}s{index}'
                    amount = rng.choice([4000, 6000, 8000, 12000])
                    add('ledger.sale', id=sale, opportunity=key, person=person, cents=amount, happened_at=day(week * 7 + 1), full_price=index % 4 != 0, complaint=rng.random() < 0.08)
                    if rng.random() < 0.15:
                        add('ledger.refund', id=f'{sale}r', sale=sale, cents=amount // 2, happened_at=day(week * 7 + 5))
    return operations

def main():
    import argparse
    parser = argparse.ArgumentParser(description='生成本地合成业务记录，不包含真实个人资料')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from pathlib import Path
    Path(args.output).write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + '\n')

if __name__=='__main__':
    main()
