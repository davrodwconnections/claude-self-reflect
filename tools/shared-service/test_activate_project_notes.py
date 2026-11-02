"""Exercise --apply success/recovery with injected tasks/HTTP and isolated files."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('notes_activation', HERE/'activate_project_notes.py')
activation = importlib.util.module_from_spec(spec); spec.loader.exec_module(activation)


class FakeOps(activation.Operations):
    def __init__(self, base, cfg):
        super().__init__(base, cfg)
        self.events=[]; self.running=True; self.worker=True; self.generation=1
        self.enabled={name:True for name in ('fixture-import','fixture-rebuild')}
        self.fail_post=False; self.fail_apply=None; self.fail_restore=None
        self.restore_failures=0; self.fail_enable=None; self.concurrent=False
        self.identity_error=False; self.mutate_candidate=False
        self.original={p:p.read_bytes() for p in base.iterdir() if p.is_file()}
    def export(self, binary, argument):
        if argument=='--catalog':return json.dumps([{'name':name} for name in ('csr_project_notes','csr_save_project_note')]).encode()
        if self.mutate_candidate:binary.write_bytes(b'changed-after-review')
        return (HERE.parents[1]/'csr-engine/data/PROJECT_NOTES_POLICY.txt').read_bytes()
    def task_info(self,name):
        return {'Execute':str(self.base/'.venv/Scripts/pythonw.exe'),'Arguments':'"'+str(self.base/'service.py')+'"',
            'State':'Running' if name=='fixture-service' and self.running else 'Ready',
            'Enabled':self.enabled.get(name,True)}
    def task(self,command,name):
        self.events.append((command,name))
        if command=='Disable-ScheduledTask':self.enabled[name]=False
        elif command=='Enable-ScheduledTask':
            if name==self.fail_enable:raise PermissionError('fixture enable failure')
            self.enabled[name]=True
        elif command=='Stop-ScheduledTask':self.running=False
        elif command=='Start-ScheduledTask':self.running=True;self.generation+=1;self.worker=False
    def processes(self,status,target):
        if self.identity_error:raise RuntimeError('fixture identity mismatch')
        return [status.get('gateway_pid'),status.get('worker_pid'),self.generation*100]
    def wait(self,processes):
        assert not self.running
        self.events.append(('wait-process-exit',tuple(processes)))
    def protect(self,path):path.mkdir()
    def backup(self,db,backup):
        result=super().backup(db,backup)
        if self.concurrent:(self.base/'service.py').write_bytes(b'concurrent-owner-change')
        return result
    def write(self,path,data,expected=None):
        original=self.original.get(path)
        restoring=original is not None and data==original
        self.events.append(('restore' if restoring else 'install',path.name))
        if restoring and self.fail_restore==path.name and self.restore_failures:
            self.restore_failures-=1;raise PermissionError('fixture locked image')
        if not restoring and self.fail_apply==path.name:
            self.fail_apply=None;raise OSError('fixture install failure')
        if expected is not None and activation.digest(path)!=expected:raise RuntimeError('fixture concurrent write refused')
        path.write_bytes(data)
    def request(self,path,body=None):
        if not self.running:raise ConnectionRefusedError('fixture service stopped')
        if path=='/status':return {'busy':False,'queued_imports':0,'gateway_pid':self.generation*100+1,
            'worker_pid':self.generation*100+2 if self.worker else None}
        method=body['method']
        if method=='initialize':return {'result':{'instructions':self.export(None,'--notes-policy').decode()}}
        if method=='tools/list':return {'result':{'tools':[{'name':name} for name in ('csr_project_notes','csr_save_project_note')]}}
        name=body['params']['name'];self.worker=True
        self.events.append(('tool',name))
        if name=='csr_project_notes' and self.fail_post:return {'result':{'isError':True,'content':[{'type':'text','text':'fixture failed DB load'}]}}
        return {'result':{'content':[{'type':'text','text':'fixture ok'}]}}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='csr-activation-test-')
        self.root=pathlib.Path(self.temporary.name)
        self.base=self.root/'service';self.base.mkdir();(self.base/'backups').mkdir()
        self.home=self.root/'profile'
        for name in ('.codex/hooks.json','.claude/settings.json'):
            p=self.home/name;p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(json.dumps({'untouched':True,'hooks':{'SessionStart':[{'hooks':[{'command':'existing'}]}]}}))
        for name in ('service.py','hook.py','csr-shared-worker.exe','catalog.json'):(self.base/name).write_bytes(('original-'+name).encode())
        self.db=self.base/'csr-engine.db'
        conn=sqlite3.connect(self.db)
        conn.executescript('CREATE TABLE chunks(id TEXT);CREATE TABLE reflections(id TEXT);INSERT INTO chunks VALUES("fixture");')
        conn.commit();conn.close()
        self.cfg={'binary':str(self.base/'csr-shared-worker.exe'),'db_path':str(self.db),'port':1,'token':'fixture-not-a-real-secret'}
        (self.base/'config.json').write_text(json.dumps(self.cfg))
        self.binary=self.root/'candidate.exe';self.binary.write_bytes(b'reviewed-candidate')
        self.sha=activation.digest(self.binary)
        self.ops=FakeOps(self.base,self.cfg)
    def tearDown(self):self.temporary.cleanup()
    def run_activation(self,apply=True,sha=None):
        with redirect_stdout(io.StringIO()):
            return activation.main(self.binary,apply,sha if sha is not None else self.sha,
                base=self.base,home=self.home,operations=self.ops,task_name='fixture-service',
                pause_tasks=('fixture-import','fixture-rebuild'))
    def receipt(self):return json.loads(next((self.base/'backups').glob('*/activation-receipt.json')).read_text())
    def test_dry_run_checks_runtime_identity_before_backups(self):
        self.ops.identity_error=True
        with self.assertRaises(RuntimeError):self.run_activation(False)
        self.assertEqual(list((self.base/'backups').iterdir()),[])
        self.assertEqual(self.ops.events,[])
    def test_hash_required_and_mismatch_prevents_any_changes(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            activation.main(self.binary,True,base=self.base,home=self.home,operations=self.ops)
        with self.assertRaises(RuntimeError):self.run_activation(sha='0'*64)
        self.assertEqual(self.ops.events,[])
    def test_staged_hash_is_checked(self):
        self.ops.mutate_candidate=True
        with self.assertRaises(RuntimeError):self.run_activation()
        self.assertFalse(any(event[0]=='Disable-ScheduledTask' for event in self.ops.events))
    def test_success_opens_live_worker_and_preserves_credentials(self):
        before=(self.base/'config.json').read_bytes()
        result=self.run_activation()
        self.assertTrue(result['activated']);self.assertTrue(result['live_worker_opened'])
        self.assertEqual((self.base/'config.json').read_bytes(),before)
        self.assertEqual(result['worker_pid'],202)
        self.assertIn(('tool','csr_project_notes'),self.ops.events)
        self.assertIn(('tool','csr_reflect_on_past'),self.ops.events)
        self.assertTrue(all(self.ops.enabled.values()))
        for path in (self.home/'.codex/hooks.json',self.home/'.claude/settings.json'):
            data=json.loads(path.read_bytes());self.assertTrue(data['untouched'])
            self.assertEqual(len(data['hooks']['SessionStart']),2)
        self.assertEqual(self.receipt()['candidate_sha256'],self.sha)
    def test_failed_postflight_waits_then_restores_and_restarts(self):
        self.ops.fail_post=True
        with self.assertRaises(RuntimeError):self.run_activation()
        receipt=self.receipt()
        self.assertFalse(receipt['activated']);self.assertFalse(receipt['rollback_failed'])
        self.assertTrue(receipt['service_restarted']);self.assertTrue(all(self.ops.enabled.values()))
        waits=[i for i,event in enumerate(self.ops.events) if event[0]=='wait-process-exit']
        first_restore=next(i for i,event in enumerate(self.ops.events) if event[0]=='restore')
        self.assertEqual(len(waits),2);self.assertLess(waits[1],first_restore)
        for path,old in self.ops.original.items():
            self.assertEqual(path.read_bytes(),old)
    def test_partial_install_failure_restores_previous_assets(self):
        self.ops.fail_apply='hook.py'
        with self.assertRaises(RuntimeError):self.run_activation()
        self.assertFalse(self.receipt()['rollback_failed'])
        self.assertTrue(self.ops.running)
        self.assertEqual((self.base/'service.py').read_bytes(),b'original-service.py')
    def test_restore_retries_and_independent_failures(self):
        self.ops.fail_post=True;self.ops.fail_restore='csr-shared-worker.exe';self.ops.restore_failures=3
        with self.assertRaises(RuntimeError):self.run_activation()
        receipt=self.receipt()
        self.assertTrue(receipt['rollback_failed']);self.assertTrue(receipt['service_restarted'])
        self.assertEqual((self.base/'service.py').read_bytes(),b'original-service.py')
        self.assertTrue(any('csr-shared-worker.exe' in error for error in receipt['rollback_errors']))
        self.assertEqual(len(receipt['tasks_left_paused']),2)
        self.assertEqual(sum(event==('restore','csr-shared-worker.exe') for event in self.ops.events),3)
    def test_one_enable_failure_does_not_skip_other_task(self):
        self.ops.fail_enable='fixture-import'
        with self.assertRaises(RuntimeError):self.run_activation()
        receipt=self.receipt();self.assertFalse(receipt['activated'])
        self.assertTrue(self.ops.enabled['fixture-rebuild'])
        self.assertEqual(receipt['tasks_left_paused'],['fixture-import'])
    def test_concurrent_asset_is_not_overwritten(self):
        self.ops.concurrent=True
        with self.assertRaises(RuntimeError):self.run_activation()
        self.assertEqual((self.base/'service.py').read_bytes(),b'concurrent-owner-change')
        self.assertFalse(any(event[0]=='install' for event in self.ops.events))
    def test_venv_core_launcher_and_worker_are_all_verified(self):
        venv=self.base/'.venv';venv.mkdir();core=self.root/'Python311'
        (venv/'pyvenv.cfg').write_text('home = '+str(core))
        script=str(self.base/'service.py');launcher=venv/'Scripts/pythonw.exe'
        class Process:
            def __init__(self,pid,exe,parent=None):self.pid=pid;self._exe=exe;self._parent=parent;self._children=[]
            def exe(self):return str(self._exe)
            def cmdline(self):return [str(self._exe),script]
            def parent(self):return self._parent
            def children(self):return self._children
            def ppid(self):return self._parent.pid
        launch=Process(100,launcher);gateway=Process(101,core/'pythonw.exe',launch)
        worker=Process(102,self.base/'csr-shared-worker.exe',gateway);gateway._children=[worker]
        processes={p.pid:p for p in (launch,gateway,worker)}
        result=activation.verified_processes({'gateway_pid':101,'worker_pid':102},self.base,self.base/'csr-shared-worker.exe',processes.__getitem__)
        self.assertEqual({p.pid for p in result},{100,101,102})
        gateway._exe=self.root/'unrelated/pythonw.exe'
        with self.assertRaises(RuntimeError):
            activation.verified_processes({'gateway_pid':101},self.base,self.base/'csr-shared-worker.exe',processes.__getitem__)


if __name__=='__main__':unittest.main(verbosity=2)
