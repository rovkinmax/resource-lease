from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from resourcelease.admission import Admission, WAIT_HEARTBEAT
from resourcelease.model import LeaseError


_CLAIM = """
import json
from pathlib import Path
import sys
from resourcelease.admission import Admission
authority = Admission(Path(sys.argv[1]))
request = authority.enqueue()
if not authority.try_admit(request):
    print(json.dumps([request, False]), flush=True)
else:
    print(json.dumps([request, True]), flush=True)
    sys.stdin.read()
"""


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.admission = Admission.initialize(self.root)

    def fill(self):
        active = [self.admission.enqueue() for _ in range(3)]
        for request in active:
            self.assertTrue(self.admission.try_admit(request))
        return active

    def test_fifo_waiting_and_cancel_across_authority_instances(self):
        active = self.fill()
        other = Admission(self.root)
        first, second, third = (other.enqueue() for _ in range(3))
        self.assertFalse(other.try_admit(first))
        self.admission.finish(active[0], cleanup_confirmed=True)
        self.assertFalse(other.try_admit(second), "cannot jump the oldest request")
        other.cancel_waiting(first)
        self.assertFalse(other.try_admit(third))
        self.assertTrue(other.try_admit(second))
        self.assertFalse(other.try_admit(third))
        with self.assertRaisesRegex(LeaseError, "no longer waiting"):
            other.try_admit(second)

    def test_clock_expiry_evicts_waiters_but_keeps_all_admitted_claims(self):
        active = self.fill()
        abandoned = self.admission.enqueue()
        with patch("resourcelease.admission.time.monotonic", return_value=(
            time.monotonic() + WAIT_HEARTBEAT + 1
        )):
            fresh = self.admission.enqueue()
            self.assertFalse(self.admission.try_admit(fresh))
            with self.assertRaisesRegex(LeaseError, "no longer waiting"):
                self.admission.try_admit(abandoned)
            self.admission.finish(active[0], cleanup_confirmed=True)
            self.assertTrue(self.admission.try_admit(fresh))

    def test_uncertain_cleanup_remains_counted_and_cannot_be_owner_released(self):
        active = self.fill()
        self.admission.finish(active[0], cleanup_confirmed=False)
        with self.assertRaisesRegex(LeaseError, "without an admitted lease"):
            self.admission.finish(active[0], cleanup_confirmed=True)
        waiting = self.admission.enqueue()
        self.assertFalse(self.admission.try_admit(waiting))

    def test_parallel_processes_and_lost_admitted_owner_keep_global_count(self):
        processes = []
        for _ in range(8):
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", _CLAIM, str(self.root)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(self.reap, process)
            processes.append(process)
        claims = []
        for process in processes:
            request, admitted = json.loads(process.stdout.readline())
            if admitted:
                claims.append((process, request))
        # Unsuccessful clients may have arrived before the oldest caller had
        # claimed; fill to three deterministically after explicitly cancelling.
        active = [request for _, request in claims]
        self.assertLessEqual(len(active), 3)
        self.assertTrue(active)
        for process in processes:
            if all(process is not owner for owner, _ in claims):
                self.assertEqual(process.wait(timeout=5), 0)
        # A restart/reopen of the authority after the owner vanishes must not
        # release that claim on its own; child cleanup was not attested.
        process, request = claims[0]
        process.kill()
        process.wait(timeout=5)
        with Admission(self.root)._transaction(write=False) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM requests WHERE id=?", (request,),
            ).fetchone(), ("admitted",))

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_open_cannot_create_missing_database_and_initialize_refuses_overwrite(self):
        with self.assertRaises(FileExistsError):
            Admission.initialize(self.root)
        self.admission.path.unlink()
        with self.assertRaises(FileNotFoundError):
            Admission(self.root)
        self.assertFalse(self.admission.path.exists())
