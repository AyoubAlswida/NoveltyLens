"""Local readiness only. No calls to metered providers."""
import json
import os
import subprocess
import urllib.request
from pathlib import Path


def check():
    state = json.loads(Path('/tmp/novelty-processes.json').read_text())
    for pid in state.values():
        os.kill(pid, 0)
    subprocess.run(['pg_isready', '-h', '127.0.0.1', '-U', 'agenticai',
                    '-d', 'agenticai'], check=True, stdout=subprocess.DEVNULL,
                   timeout=3)
    for url in ('http://127.0.0.1:6333/readyz',
                'http://127.0.0.1:8080/readyz', 'http://127.0.0.1:8080/'):
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError('Local service is not ready')


if __name__ == '__main__':
    try:
        check()
    except Exception:
        raise SystemExit(1)
