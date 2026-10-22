import unittest
from unittest.mock import MagicMock
from cron.scheduler import _final_response_from_result

class GuardrailCronFailureTests(unittest.TestCase):
    def test_controlled_halt_is_failure_even_if_completed(self):
        for reason,guard in [('guardrail_halt',None),('text_response',{'action':'halt','code':'repetition'}),('text_response',{'action':'block'})]:
            result={'completed':True,'failed':False,'turn_exit_reason':reason,'final_response':'I stopped retrying because I kept running read_file 8 times without making progress.'}
            if guard:result['guardrail']=guard
            with self.subTest(reason=reason,guard=guard),self.assertRaisesRegex(RuntimeError,'guardrail'):
                _final_response_from_result(result,'test','test',MagicMock())
    def test_normal_result_unchanged(self):
        self.assertEqual(_final_response_from_result({'completed':True,'failed':False,'final_response':'SYNC_OK'},'test','test',MagicMock()),'SYNC_OK')
if __name__=='__main__':unittest.main(verbosity=2)
