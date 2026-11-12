"""Own all five services; any exited service stops the whole container."""
import json
import os
import pwd
import glob
import tempfile
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

processes = {}
stopping = False
trial = os.environ.get('NOVELTY_DATAROBOT_TRIAL') == '1'


def as_user(user, command):
    return command if trial else ['gosu', user, *command]


def log(message):
    print(f'[single-image] {message}', flush=True)


def request_stop(signum, frame):
    global stopping
    stopping = True


def start(name, command, **kwargs):
    log(f'Starting {name}')
    processes[name] = subprocess.Popen(command, start_new_session=True, **kwargs)
    return processes[name]


def ensure_alive():
    if stopping:
        raise InterruptedError('Shutdown requested')
    for name, process in processes.items():
        if process.poll() is not None:
            raise RuntimeError(f'{name} exited with code {process.returncode}')


def wait_ready(name, probe, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ensure_alive()
        if probe():
            log(f'{name} ready')
            return
        time.sleep(0.5)
    raise RuntimeError(f'{name} did not become ready within {timeout}s')


def http_ready(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, TimeoutError):
        return False


def run_checked(command):
    # Also watch startup/migration commands so docker stop stays responsive.
    process = start('setup', command)
    while process.poll() is None:
        if stopping:
            raise InterruptedError('Shutdown requested')
        for name, service in processes.items():
            if name != 'setup' and service.poll() is not None:
                raise RuntimeError(f'{name} exited during setup')
        time.sleep(0.2)
    del processes['setup']
    if process.returncode:
        raise RuntimeError(f'Setup command {command[0]} failed ({process.returncode})')


def shutdown():
    Path('/tmp/novelty-processes.json').unlink(missing_ok=True)
    # Applications stop before their stores, allowing leases/transactions to finish.
    for name in reversed(list(processes)):
        process = processes[name]
        if process.poll() is None:
            log(f'Stopping {name}')
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def main():
    if os.environ.get('NOVELTY_BUILD_ID'):
        log('Build: ' + os.environ['NOVELTY_BUILD_ID'])
    if trial:
        # Platforms may override USER with a UID absent from /etc/passwd.
        # PostgreSQL initdb requires that UID to resolve to a user name.
        try:
            pwd.getpwuid(os.geteuid())
        except KeyError:
            libraries = glob.glob('/usr/lib/*/libnss_wrapper.so')
            if not libraries or os.environ.get('NOVELTY_NSS_READY') == '1':
                raise RuntimeError('Runtime UID has no passwd entry; libnss_wrapper is required')
            directory = Path(tempfile.mkdtemp(prefix='novelty-nss-'))
            passwd = directory / 'passwd'
            group = directory / 'group'
            passwd.write_text(Path('/etc/passwd').read_text() +
                f'novelty-runtime:x:{os.geteuid()}:{os.getegid()}:Trial user:/tmp:/bin/sh\n')
            group.write_text(Path('/etc/group').read_text() +
                f'novelty-runtime:x:{os.getegid()}:\n')
            env = dict(os.environ, NSS_WRAPPER_PASSWD=str(passwd),
                NSS_WRAPPER_GROUP=str(group), NOVELTY_NSS_READY='1',
                LD_PRELOAD=(os.environ.get('LD_PRELOAD', '') + ' ' + libraries[0]).strip())
            os.execve(sys.executable, [sys.executable, *sys.argv], env)
    baked = Path('/opt/novelty-lens/runtime-settings.json')
    if baked.exists():
        os.environ.update(json.loads(baked.read_text()))
    if trial:
        if os.geteuid() == 0:
            raise RuntimeError('DataRobot trial must run as a non-root user')
        # DataRobot custom-app string parameters use this prefix. Copy only
        # known settings; never import arbitrary platform environment variables.
        for key in ('DEPLOYMENT_PROFILE', 'LLM_API_URL', 'LLM_MODEL', 'LLM_API_KEY', 'FUSE_API_KEY',
                    'EPO_KEY', 'EPO_SECRET', 'EPO_CONSUMER_KEY', 'EPO_CONSUMER_SECRET',
                    'OPENAI_API_KEY', 'EMBEDDING_API_URL', 'EMBEDDING_MODEL',
                    'EMBEDDING_API_KEY', 'EMBEDDING_API_FORMAT', 'LENS_API_KEY',
                    'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'REQUESTS_CA_BUNDLE',
                    'SSL_CERT_FILE'):
            parameter = 'MLOPS_RUNTIME_PARAM_' + key
            if parameter in os.environ:
                os.environ[key] = os.environ[parameter]
        log('TRIAL: all accounts, history and workflows are temporary in /tmp')
    # Never inherit a host .env or an externally addressed store in this target.
    for key, value in {
        'DATABASE_URL': 'postgresql://agenticai@127.0.0.1:5432/agenticai',
        'QDRANT_URL': 'http://127.0.0.1:6333',
        'QDRANT_API_KEY': '',
        'WORKFLOW_EXECUTOR': 'worker',
        'WORKFLOW_GRAPH_ADAPTER': 'graph.adapter:ADAPTER',
    }.items():
        os.environ[key] = value
    profile = os.environ.get('DEPLOYMENT_PROFILE', '')
    if profile not in ('aramco', 'local', 'fuse'):
        raise RuntimeError('DEPLOYMENT_PROFILE must explicitly be aramco, local or fuse')
    required = ['EMBEDDING_API_URL', 'EMBEDDING_MODEL', 'EMBEDDING_API_KEY']
    if not (os.environ.get('EPO_KEY') or os.environ.get('EPO_CONSUMER_KEY')):
        raise RuntimeError('Set EPO_KEY or EPO_CONSUMER_KEY')
    if not (os.environ.get('EPO_SECRET') or os.environ.get('EPO_CONSUMER_SECRET')):
        raise RuntimeError('Set EPO_SECRET or EPO_CONSUMER_SECRET')
    if profile == 'fuse':
        required += ['LLM_API_URL', 'LLM_MODEL', 'FUSE_API_KEY']
    else:
        required += ['LLM_API_URL', 'LLM_MODEL', 'LLM_API_KEY'] if profile == 'aramco' else ['OPENAI_API_KEY']
    for key in required:
        if not os.environ.get(key, '').strip() or os.environ[key].startswith('REPLACE_'):
            raise RuntimeError(f'Set {key} in runtime.env before starting')
    if os.environ.get('EMBEDDING_API_FORMAT') not in ('openai', 'huggingface'):
        raise RuntimeError('Set EMBEDDING_API_FORMAT to openai or huggingface')

    data = Path('/tmp/novelty-trial' if trial else '/data')
    data.mkdir(exist_ok=True)
    secret = data / 'auth-secret'
    if not os.environ.get('AUTH_SECRET'):
        if not secret.exists():
            secret.write_text(secrets.token_urlsafe(48))
            secret.chmod(0o600)
        os.environ['AUTH_SECRET'] = secret.read_text().strip()
    pgdata = data / 'postgres'
    pgdata.mkdir(exist_ok=True)
    if not trial:
        run_checked(['chown', '-R', 'postgres:postgres', str(pgdata)])
    if not (pgdata / 'PG_VERSION').exists():
        # Trust applies ONLY inside this container: no DB port is published,
        # PostgreSQL binds loopback, and pgdata is owner-only.
        run_checked(as_user('postgres', ['initdb', '-D', str(pgdata),
                     '--username=agenticai', '--auth-local=trust', '--auth-host=trust']))
    start('postgres', as_user('postgres', ['postgres', '-D', str(pgdata),
                       '-c', 'listen_addresses=127.0.0.1',
                       '-c', 'unix_socket_directories=' + str(data if trial else '/run/postgresql')]))
    wait_ready('postgres', lambda: subprocess.run(
        ['pg_isready', '-h', '127.0.0.1', '-U', 'agenticai'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    # Only create an absent database. Never replace existing reports.
    exists = subprocess.check_output(['psql', '-h', '127.0.0.1', '-U', 'agenticai',
                                      '-d', 'postgres', '-tAc',
                                      "SELECT 1 FROM pg_database WHERE datname='agenticai'"])
    if exists.strip() != b'1':
        run_checked(['createdb', '-h', '127.0.0.1', '-U', 'agenticai', 'agenticai'])

    for directory in ('qdrant', 'snapshots'):
        path = data / directory
        path.mkdir(exist_ok=True)
        if not trial:
            run_checked(['chown', '-R', 'novelty:novelty', str(path)])
    qenv = dict(os.environ, LD_LIBRARY_PATH='/opt/qdrant/lib',
                QDRANT__STORAGE__STORAGE_PATH=str(data / 'qdrant'),
                QDRANT__STORAGE__SNAPSHOTS_PATH=str(data / 'snapshots'),
                QDRANT__AUDIT__DIR=str(data / 'qdrant/audit'),
                QDRANT__SERVICE__HOST='127.0.0.1',
                QDRANT__TELEMETRY_DISABLED='true')
    start('qdrant', as_user('novelty', ['/opt/qdrant/qdrant', '--disable-telemetry']),
          cwd='/opt/qdrant', env=qenv)
    wait_ready('qdrant', lambda: http_ready('http://127.0.0.1:6333/readyz'))
    run_checked(as_user('novelty', ['python', 'migrate.py']))
    run_checked(as_user('novelty', ['python', 'checkpointer.py', 'setup']))
    if os.environ.get('NOVELTY_RENDER_FREE') == '1':
        account_env = dict(os.environ)
        for key in ('NOVELTY_LOGIN_EMAIL', 'NOVELTY_LOGIN_PASSWORD_HASH'):
            account_env[key] = os.environ.pop(key, '')
        run_checked(as_user('novelty', ['python', 'provision_render_account.py']),
                    env=account_env)
    start('api', as_user('novelty', ['python', '-m', 'uvicorn', 'server:app',
                  '--host', '127.0.0.1', '--port', '8001']))
    wait_ready('api', lambda: http_ready('http://127.0.0.1:8001/readyz'))
    start('worker', as_user('novelty', ['python', '-m', 'workflow.worker']))
    start('nginx', ['nginx', '-g', 'daemon off;'])
    wait_ready('frontend', lambda: http_ready('http://127.0.0.1:8080/'))
    Path('/tmp/novelty-processes.json').write_text(json.dumps(
        {name: process.pid for name, process in processes.items()}))
    log('Ready: DataRobot trial on port 8080' if trial else
        'Ready: open http://localhost:4200 (default host port)')
    while not stopping:
        ensure_alive()
        time.sleep(0.5)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    code = 0
    try:
        main()
    except InterruptedError:
        pass
    except Exception as error:
        # Startup errors describe names/steps, never environment values.
        log(f'Startup/service failure: {type(error).__name__}')
        if isinstance(error, PermissionError):
            log(f'Permission denied accessing {error.filename}; runtime uid={os.geteuid()} gid={os.getegid()}')
        if isinstance(error, RuntimeError):
            log(str(error))
        code = 1
    finally:
        shutdown()
    sys.exit(code)
