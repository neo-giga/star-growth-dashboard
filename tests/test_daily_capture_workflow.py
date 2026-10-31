"""The daily capture workflow: GITHUB_TOKEN only, contents: write on its one job only, no Pages, its own concurrency group, the
day's files committed before the run can go red, and the commit step's shell exercised on a real git remote (orphan start,
existing branch, a failed fetch, a concurrent push)."""
import os, pathlib, re, shutil, subprocess, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/daily-capture.yml').read_text()
WEEKLY = (ROOT / '.github/workflows/publish-page.yml').read_text()


def step_script(name):
    return re.search(rf'      - name: {re.escape(name)}\n(?:        (?!run: \|).*\n)*        run: \|\n((?:          .*\n)+)', WORKFLOW)[1]


class DailyCaptureWorkflowTests(unittest.TestCase):
    def test_docs_copy_is_byte_identical(self):
        self.assertEqual((ROOT / 'docs/daily-capture.yml').read_bytes(), (ROOT / '.github/workflows/daily-capture.yml').read_bytes())

    def test_github_token_only_and_contents_write_on_the_capture_job_only(self):
        self.assertEqual(re.findall(r'\$\{\{ *secrets\.[A-Za-z_]+ *\}\}', WORKFLOW), [])
        self.assertEqual(re.findall(r'\$\{\{ *(github\.token|secrets\.GITHUB_TOKEN) *\}\}', WORKFLOW), ['github.token'])
        self.assertIn('GH_TOKEN: ${{ github.token }}', WORKFLOW)
        self.assertNotRegex(WORKFLOW, r'\bPAT\b|(?i:personal.access|STARS_TOKEN)')
        top, job = WORKFLOW.split('jobs:\n', 1)
        self.assertRegex(top, r'\npermissions:\n  contents: read\n')
        self.assertEqual(job.count('contents: write'), 1); self.assertEqual(WORKFLOW.count('contents: write'), 1)
        self.assertRegex(job, r'^  capture:\n(?:    .*\n)*?    permissions:\n      contents: write  #.*\n    steps:\n')
        self.assertEqual(re.findall(r'^  \w+:\n', job, re.M), ['  capture:\n'])  # one job
        self.assertNotRegex(job, r'\n      (pages|id-token|actions): ')

    def test_no_pages_step_and_a_separate_concurrency_group(self):
        for forbidden in ('deploy-pages', 'upload-pages-artifact', 'configure-pages', 'environment:', 'github-pages', 'pages: write'):
            self.assertNotIn(forbidden, WORKFLOW)
        group = re.search(r'\nconcurrency:\n  group: (.*)\n  cancel-in-progress: false\n', WORKFLOW)[1]
        self.assertEqual(group, 'daily-star-capture'); self.assertNotEqual(group, re.search(r'\n  group: (.*)\n', WEEKLY)[1])
        self.assertIn("    - cron: '30 5 * * *'", WORKFLOW)  # before the 07:23 UTC refresh
        self.assertIn("github.ref == 'refs/heads/main'", WORKFLOW); self.assertIn("github.repository == 'neo-giga/star-growth-dashboard'", WORKFLOW)
        self.assertNotIn('\n  push:', WORKFLOW)  # only the schedule and a manual dispatch
        self.assertRegex(WORKFLOW, r"      threshold:\n        description: .*\n        type: string\n        default: '2000'\n")
        self.assertRegex(WORKFLOW, r'      skip_discovery:\n        description: .*\n        type: boolean\n        default: false\n')
        self.assertIn('--threshold "$THRESHOLD" $SKIP_DISCOVERY --workers 4 --deadline-minutes 75', WORKFLOW)
        self.assertIn('python3 -m unittest discover -s tests', WORKFLOW)

    def test_the_days_files_are_committed_whatever_the_exit_code_and_the_run_goes_red_after(self):
        job = WORKFLOW.split('    steps:\n', 1)[1]
        names = re.findall(r'      - name: (.*)\n', job)
        capture, commit, keep, red = (names.index(n) for n in ('Capture today\'s star counts', 'Commit the day\'s files to the data branch',
                                                               'Preserve the diagnostic (and an unpushed commit)', 'Fail the run when coverage is under the floor'))
        self.assertLess(capture, commit); self.assertLess(commit, keep); self.assertLess(keep, red); self.assertEqual(red, len(names) - 1)
        capture_step = step_script("Capture today's star counts")
        self.assertIn('set +e', capture_step); self.assertIn('echo "code=$code" >> "$GITHUB_OUTPUT"', capture_step); self.assertTrue(capture_step.rstrip().endswith('exit 0'))
        self.assertIn('--diagnostic "$RUNNER_TEMP/capture-diagnostic.json"', capture_step)
        self.assertIn("      - name: Commit the day's files to the data branch\n        if: always()\n", job)
        self.assertIn('      - name: Preserve the diagnostic (and an unpushed commit)\n        if: always()\n', job)
        self.assertIn("      - name: Fail the run when coverage is under the floor\n        if: steps.capture.outputs.code != '0'\n", job)
        self.assertEqual(re.findall(r'git push [^\n]*', WORKFLOW), ['git push origin HEAD:refs/heads/data; then exit 0; fi'])  # the data branch only
        self.assertIn('timeout-minutes: 110', WORKFLOW)

    def test_the_checkout_and_commit_steps_work_on_a_real_remote(self):
        checkout, commit = step_script('Check out the data branch, or start it'), step_script("Commit the day's files to the data branch")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            # A clean runner: no identity from the environment or any config file, and none guessed from the host name.
            env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_', 'GIT_CONFIG'))}
            env.update(RUNNER_TEMP=str(tmp / 'runner'), HOME=str(tmp), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_COUNT='1',
                       GIT_CONFIG_KEY_0='user.useConfigOnly', GIT_CONFIG_VALUE_0='true')
            (tmp / 'runner').mkdir()
            git = lambda *a, cwd=tmp / 'work': subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git')], check=True)
            (tmp / 'work').mkdir(); git('init', '-q', '-b', 'main'); (tmp / 'work/README').write_text('x'); git('add', '-A'); git('commit', '-qm', 'main')
            git('remote', 'add', 'origin', (tmp / 'origin.git').as_uri()); git('push', '-q', 'origin', 'main')
            run = lambda script: subprocess.run(['bash', '-e', '-c', script], cwd=tmp / 'work', capture_output=True, text=True, env=env)
            # 1. no data branch yet (ls-remote exit 2): an orphan worktree; the first capture creates the branch
            proc = run(checkout); self.assertEqual(proc.returncode, 0, proc.stderr)
            (tmp / 'work/data-branch/snapshots').mkdir(); (tmp / 'work/data-branch/snapshots/d1').write_text('1')
            proc = run(commit); self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(git('ls-tree', '--name-only', '-r', 'data', cwd=tmp / 'origin.git'), 'snapshots/d1')
            self.assertEqual(git('rev-list', '--count', 'data', cwd=tmp / 'origin.git'), '1')
            self.assertNotIn('README', git('ls-tree', '--name-only', '-r', 'data', cwd=tmp / 'origin.git'))  # orphan: no main history
            # 2. a fresh clone with the branch present: detached worktree on its tip; nothing to commit is fine
            subprocess.run(['git', 'clone', '-q', '--depth=1', (tmp / 'origin.git').as_uri(), str(tmp / 'work2')], check=True, env=env)
            run2 = lambda script: subprocess.run(['bash', '-e', '-c', script], cwd=tmp / 'work2', capture_output=True, text=True, env=env)
            proc = run2(checkout); self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp / 'work2/data-branch/snapshots/d1').read_text(), '1')
            proc = run2(commit); self.assertEqual(proc.returncode, 0, proc.stderr); self.assertIn('nothing to commit', proc.stdout)
            # 2b. the branch exists but every fetch fails: the step fails after three tries, with no orphan worktree, and nothing is pushed
            (tmp / 'bin').mkdir(); (tmp / 'bin/git').write_text(f'#!/bin/bash\nif [ "$1" = fetch ]; then echo x >> {tmp}/fetches; echo "fatal: simulated network failure" >&2; exit 128; fi\nexec {shutil.which("git")} "$@"\n')
            (tmp / 'bin/sleep').write_text('#!/bin/sh\n'); (tmp / 'bin/git').chmod(0o755); (tmp / 'bin/sleep').chmod(0o755)
            subprocess.run(['git', 'clone', '-q', '--depth=1', (tmp / 'origin.git').as_uri(), str(tmp / 'work3')], check=True, env=env)
            tip = git('rev-parse', 'data', cwd=tmp / 'origin.git')
            run3 = lambda script: subprocess.run(['bash', '-e', '-c', script], cwd=tmp / 'work3', capture_output=True, text=True, env={**env, 'PATH': f'{tmp}/bin:{env["PATH"]}'})
            proc = run3(checkout); self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr); self.assertIn('not starting an empty one', proc.stderr)
            self.assertEqual((tmp / 'fetches').read_text().count('x'), 3)
            self.assertFalse((tmp / 'work3/data-branch').exists()); self.assertEqual(git('branch', '--list', 'data', cwd=tmp / 'work3'), '')
            proc = run3(commit); self.assertNotEqual(proc.returncode, 0)  # the commit step runs on always(): it has no worktree to push
            self.assertEqual(git('rev-parse', 'data', cwd=tmp / 'origin.git'), tip)
            # 3. a concurrent push lands between checkout and commit (the backfill's history): today's commit is replayed on top
            (tmp / 'work/data-branch/history').mkdir(); (tmp / 'work/data-branch/history/aa.json').write_text('{}')
            git('add', '-A', cwd=tmp / 'work/data-branch'); git('commit', '-qm', 'backfill', cwd=tmp / 'work/data-branch'); git('push', '-q', 'origin', 'HEAD:refs/heads/data', cwd=tmp / 'work/data-branch')
            (tmp / 'work2/data-branch/snapshots/d2').write_text('2')
            proc = run2(commit); self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(sorted(git('ls-tree', '--name-only', '-r', 'data', cwd=tmp / 'origin.git').split()), ['history/aa.json', 'snapshots/d1', 'snapshots/d2'])
            self.assertEqual(git('rev-list', '--count', 'data', cwd=tmp / 'origin.git'), '3')
            self.assertEqual(git('log', '-1', '--format=%cn <%ce>', 'data', cwd=tmp / 'origin.git'), 'github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>')
            self.assertFalse((tmp / 'runner/unpushed-data-commit.patch').exists())
            # 4. a conflicting concurrent change leaves the commit as a patch artifact and fails
            (tmp / 'work/data-branch/snapshots/d3').write_text('theirs'); git('pull', '-q', '--rebase', 'origin', 'data', cwd=tmp / 'work/data-branch')
            git('add', '-A', cwd=tmp / 'work/data-branch'); git('commit', '-qm', 'theirs', cwd=tmp / 'work/data-branch'); git('push', '-q', 'origin', 'HEAD:refs/heads/data', cwd=tmp / 'work/data-branch')
            (tmp / 'work2/data-branch/snapshots/d3').write_text('ours')
            proc = run2(commit); self.assertEqual(proc.returncode, 1); self.assertIn('conflicts with the data branch', proc.stderr)
            self.assertIn('ours', (tmp / 'runner/unpushed-data-commit.patch').read_text())


if __name__ == '__main__':
    unittest.main()
