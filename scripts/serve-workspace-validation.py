"""Serve an explicitly isolated workspace; never starts background workers."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))


def main():
    import uvicorn
    from noodle_agent.next.api import create_app
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8012)
    args = parser.parse_args()
    library = args.library.resolve()
    if not library.is_relative_to((ROOT / '.local-only').resolve()) or not (library / 'noodle.db').exists():
        raise ValueError('Validation API requires a prepared .local-only library')
    print('Validation library: ' + str(library), flush=True)
    uvicorn.run(create_app(library), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
