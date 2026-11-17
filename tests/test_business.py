import json
import unittest
from pathlib import Path
from product.app import Application


class BusinessBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.app=Application()
        self.app.replay(json.loads((Path(__file__).parent.parent/'data'/'demo.json').read_text()))

    def tearDown(self):
        self.app.store.close()
    def test_approval_retry_keeps_coupon_count(self):
        self.app.execute('campaign.approve',dict(id='c1',reviewer='operator'))
        self.assertEqual(len(self.app.store.all('coupon')),2)

    def test_refund_history_is_preserved(self):
        history=self.app.store.history('sale','s1')
        self.assertEqual([item['payload']['refunded'] for item in history],[0,2000])
