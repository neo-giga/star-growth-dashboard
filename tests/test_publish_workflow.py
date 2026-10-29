"""The page publication workflow: GITHUB_TOKEN only, contents read-only, no git writes; after each daily capture, on a push to main of
the deploy paths, and on dispatch; the gate step red before the upload and deploy steps (B2: a stale file, low coverage and a
null-heavy column each refuse; a good file stages), the data-branch checkout on a real remote, the stale-run guard."""
import copy, datetime as dt, importlib.util, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('deploy_paths', ROOT / 'scripts/deploy_paths.py'); _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m); DEPLOY_PATHS = _m.DEPLOY_PATHS
WORKFLOW = (ROOT / '.github/workflows/publish-page.yml').read_text()
CAPTURE = (ROOT / '.github/workflows/daily-capture.yml').read_text()
FIXTURE = json.loads((ROOT / 'tests/fixtures/windows-sample.json').read_text())
SITE_FILES = ('index.html', 'app.js', 'style.css', 'llms.txt', '.nojekyll', 'docs/agent-feed.md', 'docs/publish-page.yml', 'scripts/publish_page.py', 'scripts/build_static_release.py',
              'publish/__init__.py', 'publish/stage.py', 'publish/agent_feed.py', 'capture/__init__.py', 'capture/history.py', 'capture/windows.py')


def step_script(name):
    return re.search(rf'      - name: {re.escape(name)}\n(?:        (?!run: \|).*\n)*        run: \|\n((?:          .*\n)+)', WORKFLOW)[1]


def data_branch(root, doc, total=200):
    (root / 'windows').mkdir(parents=True); (root / 'diagnostics' / doc['date'][:4]).mkdir(parents=True)
    (root / 'windows/latest.json').write_text(json.dumps(doc))
    (root / 'diagnostics' / doc['date'][:4] / f'{doc["date"]}.json').write_text(json.dumps({'date': doc['date'], 'coverage': {'total_count': total, 'total_count_source': 'search'}}))


class PublishWorkflowTests(unittest.TestCase):
    def test_docs_copy_is_byte_identical(self):
        self.assertEqual((ROOT / 'docs/publish-page.yml').read_bytes(), (ROOT / '.github/workflows/publish-page.yml').read_bytes())
        self.assertFalse((ROOT / '.github/workflows/weekly-refresh.yml').exists()); self.assertFalse((ROOT / 'docs/weekly-refresh.yml').exists())

    def test_github_token_only_nothing_committed_contents_read_only(self):
        for forbidden in ('git push', 'git commit', 'git add', 'contents: write', 'actions: write', 'secrets.', '--ignore-gate', '--published-site', 'bootstrap'):
            self.assertNotIn(forbidden, WORKFLOW)
        self.assertEqual(WORKFLOW.count('contents: read'), 2)  # top level and the job
        self.assertRegex(WORKFLOW, r'\n    permissions:\n      contents: read\n      pages: write\n      id-token: write\n')
        self.assertEqual(re.findall(r'^  \w+:\n', WORKFLOW.split('jobs:\n', 1)[1], re.M), ['  publish:\n'])
        self.assertIn("github.ref == 'refs/heads/main'", WORKFLOW); self.assertIn("github.repository == 'neo-giga/star-growth-dashboard'", WORKFLOW)
        self.assertIn('persist-credentials: false', WORKFLOW); self.assertIn('python3 -m unittest discover -s tests', WORKFLOW)

    def test_triggers_after_the_daily_capture_on_deploy_path_pushes_and_on_dispatch(self):
        on = WORKFLOW.split('permissions:', 1)[0]
        capture_name = re.search(r'^name: (.*)$', CAPTURE, re.M)[1]
        self.assertRegex(on, rf"\n  workflow_run:\n    workflows: \['{re.escape(capture_name)}'\]\n    types: \[completed\]\n")
        self.assertIn("github.event_name != 'workflow_run' || github.event.workflow_run.head_branch == 'main'", WORKFLOW)
        self.assertRegex(on, r'\n  push:\n    branches: \[main\]')
        paths = re.search(r'    paths:\n((?:      - .*\n)+)', on)[1].split()[1::2]
        self.assertEqual(tuple(paths), DEPLOY_PATHS)  # the push filter and the stale-run guard share one list
        for name in ('index.html', 'app.js', 'style.css', 'publish/**', 'scripts/**', 'docs/agent-feed.md', 'llms.txt', '.github/workflows/publish-page.yml'):
            self.assertIn(name, paths)
        self.assertNotIn('tests/**', paths); self.assertNotIn('trending/**', paths)
        self.assertRegex(on, r'\n  workflow_dispatch:\n'); self.assertNotIn('schedule:', WORKFLOW)  # the capture's completion is the schedule
        group = re.search(r'\nconcurrency:\n  group: (.*)\n  cancel-in-progress: false\n', WORKFLOW)[1]
        self.assertEqual(group, 'public-feed-publication'); self.assertNotEqual(group, re.search(r'\n  group: (.*)\n', CAPTURE)[1])

    def test_the_gate_step_fails_before_the_upload_and_deploy_steps(self):
        job = WORKFLOW.split('    steps:\n', 1)[1]
        names = [n or u for n, u in re.findall(r'      - (?:name: (.*)|uses: (.*))\n', job)]
        self.assertEqual(names, ['actions/checkout@v4', 'Unit checks (no network fixtures)', 'Read the data branch', 'Stage the page from windows/latest.json, or refuse', 'Preserve the diagnostic',
                                 'Refuse to deploy a stale run', 'Upload exact staged Pages artifact', 'Deploy exact artifact'])
        stage = step_script('Stage the page from windows/latest.json, or refuse')
        self.assertNotIn('set +e', stage); self.assertNotIn('|| true', stage); self.assertNotIn('exit 0', stage)
        self.assertIn('python3 scripts/publish_page.py --site-dir . --data-dir data-branch --output-dir "$RUNNER_TEMP/site"', stage)
        self.assertIn('--diagnostic "$RUNNER_TEMP/publish-diagnostic.json"', stage)
        for name in ('Stage the page from windows/latest.json, or refuse', 'Refuse to deploy a stale run', 'Upload exact staged Pages artifact', 'Deploy exact artifact'):
            self.assertNotIn('        if:', job.split(f'      - name: {name}\n', 1)[1].split('      - name:', 1)[0].split('      - uses:', 1)[0], name)
            self.assertNotIn('continue-on-error', job.split(f'      - name: {name}\n', 1)[1].split('      - name:', 1)[0])
        self.assertIn('      - name: Preserve the diagnostic\n        if: always()\n', job)
        self.assertRegex(job, r'uses: actions/upload-pages-artifact@v3\n        with:\n          path: \$\{\{ runner.temp \}\}/site\n')
        self.assertIn('python3 scripts/deploy_paths.py HEAD FETCH_HEAD', step_script('Refuse to deploy a stale run'))

    def test_b2_the_gate_refuses_stale_low_coverage_and_null_heavy_files_and_stages_a_good_one(self):
        stage = step_script('Stage the page from windows/latest.json, or refuse')
        now = dt.datetime.now(dt.timezone.utc)
        good = copy.deepcopy(FIXTURE); good['generated_at'] = (now - dt.timedelta(hours=2)).isoformat()
        stale = copy.deepcopy(good); stale['generated_at'] = (now - dt.timedelta(hours=37)).isoformat()
        nulls = copy.deepcopy(good); nulls['columns']['gain_90d'][:3] = [None] * 3  # 1.5%
        cases = {'stale': (stale, 200, 'over 36 h'), 'low-coverage': (good, 205, 'under 99% of the search total 205'), 'null-heavy': (nulls, 200, 'gain_90d is null for 3 of 200 rows (1.5%)'), 'good': (good, 200, None)}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            for case, (doc, total, problem) in cases.items():
                with self.subTest(case=case):
                    work = tmp / case; work.mkdir()
                    for name in SITE_FILES:
                        (work / name).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ROOT / name, work / name)
                    data_branch(work / 'data-branch', doc, total)
                    runner = tmp / f'runner-{case}'; runner.mkdir()
                    env = dict(os.environ, RUNNER_TEMP=str(runner), PATH=f'{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}')
                    proc = subprocess.run(['bash', '-e', '-c', stage], cwd=work, capture_output=True, text=True, env=env)
                    diagnostic = json.loads((runner / 'publish-diagnostic.json').read_text())
                    if problem:
                        self.assertNotEqual(proc.returncode, 0, case); self.assertEqual(diagnostic['status'], 'refused'); self.assertIn(problem, '; '.join(diagnostic['problems']))
                        self.assertFalse((runner / 'site').exists(), case); self.assertIn('current site unchanged', proc.stderr)  # the job stops here: nothing to upload or deploy
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr); self.assertEqual(diagnostic['status'], 'ok'); self.assertEqual(diagnostic['row_count'], 200)
                        self.assertTrue((runner / 'site/index.html').is_file()); self.assertTrue((runner / 'site/api/v2/catalog.json').is_file())

    def test_the_data_branch_step_reads_the_branch_tip_or_fails_without_one(self):
        checkout = step_script('Read the data branch')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
            git = lambda *a, cwd: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git')], check=True)
            (tmp / 'work').mkdir(); git('init', '-q', '-b', 'main', cwd=tmp / 'work'); (tmp / 'work/README').write_text('x'); git('add', '-A', cwd=tmp / 'work'); git('commit', '-qm', 'main', cwd=tmp / 'work')
            git('remote', 'add', 'origin', (tmp / 'origin.git').as_uri(), cwd=tmp / 'work'); git('push', '-q', 'origin', 'main', cwd=tmp / 'work')
            subprocess.run(['git', 'clone', '-q', '--depth=1', (tmp / 'origin.git').as_uri(), str(tmp / 'runner')], check=True, env=env)
            run = lambda: subprocess.run(['bash', '-e', '-c', checkout], cwd=tmp / 'runner', capture_output=True, text=True, env=env)
            proc = run(); self.assertNotEqual(proc.returncode, 0); self.assertFalse((tmp / 'runner/data-branch').exists())  # no data branch yet: red, nothing published
            (tmp / 'data').mkdir(); git('init', '-q', '-b', 'data', cwd=tmp / 'data'); (tmp / 'data/windows').mkdir(); (tmp / 'data/windows/latest.json').write_text('{}')
            git('add', '-A', cwd=tmp / 'data'); git('commit', '-qm', 'capture', cwd=tmp / 'data'); git('push', '-q', (tmp / 'origin.git').as_uri(), 'HEAD:refs/heads/data', cwd=tmp / 'data')
            proc = run(); self.assertEqual(proc.returncode, 0, proc.stderr); self.assertEqual((tmp / 'runner/data-branch/windows/latest.json').read_text(), '{}')
            self.assertFalse((tmp / 'runner/data-branch/README').exists())

    def test_the_stale_run_guard_ignores_changes_outside_the_deploy_paths(self):
        step = step_script('Refuse to deploy a stale run')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp); git = lambda *a, cwd=tmp / 'work': subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git')], check=True); (tmp / 'work').mkdir(); git('init', '-q', '-b', 'main')
            for name in ('app.js', 'tests/test_x.py', 'scripts/deploy_paths.py', '.github/workflows/publish-page.yml'):
                (tmp / 'work' / name).parent.mkdir(parents=True, exist_ok=True); (tmp / 'work' / name).write_bytes((ROOT / name).read_bytes() if (ROOT / name).exists() else b'x')
            git('add', '-A'); git('commit', '-qm', 'run'); run = git('rev-parse', 'HEAD'); git('remote', 'add', 'origin', (tmp / 'origin.git').as_uri())
            env = dict(os.environ, PATH=f'{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}')
            guard = lambda: subprocess.run(['bash', '-e', '-c', step], cwd=tmp / 'work', capture_output=True, text=True, env=env)
            def land(name, text):
                git('checkout', '-q', 'main'); (tmp / 'work' / name).write_text(text); git('commit', '-qam', name); git('push', '-q', 'origin', 'main'); git('checkout', '-q', '--detach', run)
            land('tests/test_x.py', 'y'); proc = guard(); self.assertEqual(proc.returncode, 0, proc.stderr); self.assertIn('deploy-relevant sources match live main', proc.stdout)
            land('app.js', 'changed'); proc = guard(); self.assertEqual(proc.returncode, 1); self.assertIn('newer UI/scripts on main', proc.stderr)


if __name__ == '__main__':
    unittest.main()
