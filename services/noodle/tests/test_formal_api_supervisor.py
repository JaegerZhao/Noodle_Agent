"""API process recovery is bounded and never restarts generation workers."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    path=Path(__file__).resolve().parents[3]/'scripts/formal-workspace.py'
    spec=importlib.util.spec_from_file_location('formal_runtime_supervision',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_retries_an_abrupt_exit_but_stops_after_clean_shutdown(tmp_path,monkeypatch):
    module=runner()
    codes=iter([1,0]);calls=[]
    def spawn(command,**options):
        calls.append((command,options))
        return SimpleNamespace(pid=123,wait=lambda:next(codes))
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    monkeypatch.setattr('time.sleep',lambda _:None)
    module.supervise(tmp_path,'api')
    assert len(calls)==2 and all(command[-1]=='api' for command,_ in calls)
    assert all(options['stdin']==module.subprocess.DEVNULL for _,options in calls)
    assert all(options['creationflags']==(module.subprocess.CREATE_NO_WINDOW if module.os.name=='nt' else 0) for _,options in calls)


def test_api_failure_limit_is_not_an_infinite_restart_loop(tmp_path,monkeypatch):
    module=runner();calls=[]
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:calls.append(a) or SimpleNamespace(pid=123,wait=lambda:1))
    monkeypatch.setattr('time.sleep',lambda _:None)
    with pytest.raises(SystemExit,match='three'):
        module.supervise(tmp_path,'api')
    assert len(calls)==3


def test_api_guard_cannot_restart_a_model_generation_worker(tmp_path):
    with pytest.raises(ValueError,match='Only the API'):
        runner().supervise(tmp_path,'worker')


def test_hidden_python_child_has_no_console_on_windows():
    module=runner()
    if module.os.name!='nt':
        pytest.skip('Windows console behavior')
    completed=module.subprocess.run([module.sys.executable,'-c',
        'import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow()))'],
        capture_output=True,text=True,check=True,creationflags=module.subprocess.CREATE_NO_WINDOW)
    assert completed.stdout.strip()=='False'
