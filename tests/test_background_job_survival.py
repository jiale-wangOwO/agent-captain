"""Opt-in, bounded Windows acceptance; never starts Hub, SSH, or a model.

The test owns a private nested KILL_ON_JOB_CLOSE job. Closing its last handle
kills only a temporary launch parent and its control child, while the external
monitor and the target must survive, then stop through a test-only stop file.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from agent_captain.infrastructure.process_identity import observe_process

ROOT = Path(__file__).resolve().parents[1]
PARENT = r'''
import ctypes, json, os, subprocess, sys, time
from ctypes import wintypes
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_captain.infrastructure.background_process import launch_background
from agent_captain.infrastructure.process_identity import observe_process
folder = Path(sys.argv[3])
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel.OpenJobObjectW.restype = wintypes.HANDLE
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
job = kernel.OpenJobObjectW(5, False, sys.argv[2])
if not job or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
    raise ctypes.WinError(ctypes.get_last_error())
kernel.CloseHandle(job)  # root owns the last handle, so its close is decisive
control = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(90)'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
child_code = """
import sys, time
from pathlib import Path
folder = Path(sys.argv[1])
deadline = time.monotonic() + 90
print('background-test-ready', flush=True)
while time.monotonic() < deadline and not (folder / 'stop').exists():
    (folder / 'heartbeat.tmp').write_text(str(time.monotonic()), encoding='ascii')
    (folder / 'heartbeat.tmp').replace(folder / 'heartbeat')
    time.sleep(.1)
print('background-test-stopped', flush=True)
"""
process = launch_background([sys.executable, '-I', '-u', '-c', child_code, str(folder)],
                            cwd=folder, stdout_path=folder / 'child.out.log',
                            stderr_path=folder / 'child.err.log')
result = {'parent': observe_process(os.getpid()), 'parent_pid': os.getpid(),
          'control': observe_process(control.pid), 'control_pid': control.pid,
          'child': observe_process(process.pid), 'child_pid': process.pid,
          'receipt_path': str(process.receipt_path)}
temporary = folder / 'result.tmp'
temporary.write_text(json.dumps(result), encoding='utf-8')
temporary.replace(folder / 'result.json')
time.sleep(90)
'''


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('Timed out waiting for isolated background acceptance')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('RUN_WINDOWS_BACKGROUND_LIVE') == '1',
                     'Opt-in Windows WMI/nested job acceptance only')
class BackgroundJobSurvivalTests(unittest.TestCase):
    def test_actual_child_and_monitor_survive_private_parent_job_teardown(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                        ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job_name = 'Local\\PRONHub-Acceptance-' + uuid.uuid4().hex
        job = kernel.CreateJobObjectW(None, job_name)
        self.assertTrue(job)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway allowed
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        with tempfile.TemporaryDirectory(prefix='pron-background-acceptance-') as temporary:
            folder = Path(temporary).resolve()
            parent = None
            receipt = None
            monitor_before = None
            try:
                with (folder / 'parent.out.log').open('wb') as output, (folder / 'parent.err.log').open('wb') as error:
                    parent = subprocess.Popen([sys.executable, '-I', '-u', '-c', PARENT,
                                               str(ROOT / "src"), job_name, str(folder)],
                                              stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                              creationflags=subprocess.CREATE_NO_WINDOW)

                def parent_result():
                    if parent.poll() is not None:
                        self.fail('Isolated parent exited early: ' + (folder / 'parent.err.log').read_text())
                    return (folder / 'result.json').exists()

                wait_for(parent_result, 40)
                result = json.loads((folder / 'result.json').read_text())
                receipt_path = Path(result['receipt_path'])
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt['pid'], result['child_pid'])
                self.assertEqual(receipt['birth'], result['child']['birth'])
                self.assertFalse((receipt_path.parent / 'plan.json').exists())
                monitor_before = observe_process(receipt['wrapper_pid'])
                self.assertEqual(monitor_before['state'], 'alive')
                heartbeat = folder / 'heartbeat'
                wait_for(heartbeat.exists)
                before = float(heartbeat.read_text())
                kernel.CloseHandle(job)
                job = None
                parent.wait(timeout=5)
                self.assertIsNotNone(parent.returncode)
                wait_for(lambda: observe_process(result['control_pid']) != result['control'])
                time.sleep(1)
                self.assertGreater(float(heartbeat.read_text()), before)
                self.assertEqual(observe_process(result['child_pid']), result['child'])
                self.assertEqual(observe_process(receipt['wrapper_pid']), monitor_before)
                self.assertEqual(receipt['wrapper_job']['job_flags'] & 0x2000, 0)
                self.assertEqual(receipt['route_probe']['job_flags'] & 0x2000, 0)
                (folder / 'stop').touch()
                events_path = receipt_path.parent / 'lifecycle.jsonl'

                def exited():
                    return next((entry for entry in
                                 (json.loads(line) for line in events_path.read_text().splitlines())
                                 if entry['event'] == 'child_exited'), None)

                exit_event = wait_for(exited)
                self.assertEqual(exit_event['exit_code'], 0)
                wait_for(lambda: observe_process(receipt['wrapper_pid']) != monitor_before)
                self.assertIn('background-test-stopped', (folder / 'child.out.log').read_text())
                evidence = {'verified_at': time.time(), 'private_nested_job_closed': True,
                            'parent_terminated': True, 'control_child_terminated': True,
                            'actual_child_survived': True, 'external_monitor_survived': True,
                            'heartbeat_advanced': True, 'child_pid': receipt['pid'],
                            'birth': receipt['birth'], 'wrapper_pid': receipt['wrapper_pid'],
                            'wrapper_job': receipt['wrapper_job'], 'child_job': receipt['child_job'],
                            'route_probe': receipt['route_probe'], 'voluntary_exit_code': 0}
                evidence_file = os.environ.get('PRON_BACKGROUND_TEST_EVIDENCE')
                if evidence_file:
                    destination = Path(evidence_file)
                    self.assertTrue(destination.is_absolute())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            finally:
                (folder / 'stop').touch(exist_ok=True)
                if job:
                    kernel.CloseHandle(job)
                if parent is not None:
                    parent.wait(timeout=5)
                if receipt:
                    wait_for(lambda: observe_process(receipt['pid']).get('birth') != receipt['birth'], 10)
                    wait_for(lambda: observe_process(receipt['wrapper_pid']) != monitor_before, 10)


if __name__ == '__main__':
    unittest.main()
