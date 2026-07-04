import tempfile
import time
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hermes_serial_bridge import ApiRunJob, JobManager


class FakeApiClient:
    enabled = True

    def __init__(self):
        self.started = []
        self.ran_jobs = []
        self.paused = []
        self.resumed = []
        self.jobs_enabled = True

    def start(self, prompt, session_id=None):
        self.started.append((prompt, session_id))
        return "run_fake"

    def stream_events(self, run_id):
        return iter(())

    def status(self, run_id):
        return {"status": "completed"}

    def run_job(self, job_id):
        self.ran_jobs.append(job_id)
        return {"job": {"id": job_id, "name": "nightly"}}

    def list_jobs(self, include_disabled=True):
        return [{"id": "abc123", "name": "nightly", "enabled": self.jobs_enabled}]

    def pause_job(self, job_id):
        self.paused.append(job_id)
        self.jobs_enabled = False
        return {"job": {"id": job_id, "name": "nightly", "enabled": False}}

    def resume_job(self, job_id):
        self.resumed.append(job_id)
        self.jobs_enabled = True
        return {"job": {"id": job_id, "name": "nightly", "enabled": True}}


class BridgeIntegrationConfigTests(unittest.TestCase):
    def _manager(self, actions, client=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "actions.json"
        path.write_text('{"actions": []}')
        mgr = JobManager(path, api_client=client or FakeApiClient())
        mgr.actions = actions
        return mgr

    def test_legacy_cli_action_uses_native_run_when_api_enabled(self):
        client = FakeApiClient()
        mgr = self._manager([
            {
                "id": "status",
                "label": "Status",
                "enabled": True,
                "command": ["hermes", "chat", "-q", "hello familiar"],
            }
        ], client)
        ack = mgr.start_first_enabled()
        self.assertIn("started API", ack["msg"])
        self.assertEqual(client.started[0][0], "hello familiar")
        self.assertIsInstance(mgr.job, ApiRunJob)

    def test_cron_job_action_runs_and_pause_resume_controls_schedule(self):
        client = FakeApiClient()
        mgr = self._manager([
            {"id": "nightly", "label": "Nightly", "enabled": True, "type": "cron_job", "job_id": "abc123"}
        ], client)
        ack = mgr.start_first_enabled()
        self.assertIn("triggered job", ack["msg"])
        self.assertEqual(client.ran_jobs, ["abc123"])

        pause = mgr.pause_or_resume_configured_job()
        self.assertIn("paused job", pause["msg"])
        self.assertEqual(client.paused, ["abc123"])

        resume = mgr.pause_or_resume_configured_job()
        self.assertIn("resumed job", resume["msg"])
        self.assertEqual(client.resumed, ["abc123"])

    def test_sse_event_mapping_for_device_protocol(self):
        self.assertEqual(
            ApiRunJob._device_event("tool.started", {"tool_name": "terminal"}),
            {"type": "event", "event": "tool_started", "msg": "terminal"},
        )
        perm = ApiRunJob._device_event("approval.requested", {"text": "Run command?"})
        self.assertEqual(perm["type"], "permission")
        self.assertEqual(perm["choices"], ["once", "deny"])


if __name__ == "__main__":
    unittest.main()
