"""Regression: no-agent script control failures must not be delivered as success."""
import unittest
from unittest.mock import patch
from cron import scheduler as s
class NoAgentFailureTest(unittest.TestCase):
    def run_output(self,output):
        with patch.object(s,'_resolve_job_workdir',return_value=None),patch.object(s,'_run_job_script_with_claim_heartbeat',return_value=(True,output)),patch('hermes_cli.env_loader.load_hermes_dotenv'):
            return s._run_no_agent_job({'script':'test.py'},'test','test',None)
    def test_failure_marker(self):
        result=self.run_output('[CRON_FAILURE]\nReceipt missing')
        self.assertFalse(result[0]);self.assertEqual(result[3],'Receipt missing')
    def test_normal_and_quoted_marker(self):
        for text in ('Report ready','Explanation of [CRON_FAILURE] token'):
            self.assertTrue(self.run_output(text)[0])
    def test_empty(self):self.assertTrue(self.run_output('')[0])
if __name__=='__main__':unittest.main(verbosity=2)
