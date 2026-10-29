"""Stage the page from a checkout of the data branch, or refuse (publish/stage.py is the gate).

Exit 0: staged under --output-dir, deploy it. Exit 2: refused by the gate or failed; nothing staged, the current page stays.
The diagnostic (--diagnostic) is written in every case: status ok / refused / failed, the problems, the measurements.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from publish.stage import GateRefused, stage_site


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    p.add_argument('--site-dir', required=True, help='the checkout (index.html, app.js, style.css, llms.txt, docs/)')
    p.add_argument('--data-dir', required=True, help='a checkout of the data branch (windows/latest.json, diagnostics/)')
    p.add_argument('--output-dir', required=True, help='where the site is staged; must not exist')
    p.add_argument('--diagnostic', help='also write the diagnostic JSON here')
    p.add_argument('--now', help='ISO instant the file age is measured against (tests); default: now')
    p.add_argument('--ignore-gate', action='store_true', help='stage even when the gate refuses (local previews only; the workflow never passes it)')
    a = p.parse_args(argv)
    now = dt.datetime.fromisoformat(a.now.replace('Z', '+00:00')) if a.now else None
    try:
        result = dict(stage_site(a.site_dir, a.data_dir, a.output_dir, now=now, ignore_gate=a.ignore_gate), status='ok', problems=[])
        code = 0
    except GateRefused as exc:
        result = {'status': 'refused', 'problems': exc.problems, 'measurements': exc.measurements, 'current_site_preserved': True}
        code = 2
    except Exception as exc:  # noqa: BLE001 - any failure is reported in the diagnostic, never hidden
        result = {'status': 'failed', 'problems': [f'{type(exc).__name__}: {str(exc)[:500]}'], 'current_site_preserved': True}
        code = 2
    if a.diagnostic:
        path = pathlib.Path(a.diagnostic); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if code:
        print('refused: ' + '; '.join(result['problems']) + '; current site unchanged', file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
