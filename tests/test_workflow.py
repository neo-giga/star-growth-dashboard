"""The installed workflow: no git writes, one artifact per run, a push to main of UI/source files re-publishes with validate-current."""
import importlib.util,os,pathlib,re,subprocess,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
_spec=importlib.util.spec_from_file_location('deploy_paths',ROOT/'scripts/deploy_paths.py');_m=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_m);DEPLOY_PATHS=_m.DEPLOY_PATHS
WORKFLOW=(ROOT/'.github/workflows/weekly-refresh.yml').read_text()
class WorkflowTests(unittest.TestCase):
 def test_docs_copy_is_byte_identical(self):
  self.assertEqual((ROOT/'docs/weekly-refresh.yml').read_bytes(),(ROOT/'.github/workflows/weekly-refresh.yml').read_bytes())
 def test_nothing_is_committed_or_pushed_and_contents_stay_read_only(self):
  for forbidden in ('git push','git commit','git add','contents: write'):self.assertNotIn(forbidden,WORKFLOW)
  self.assertEqual(WORKFLOW.count('contents: read'),2)  # top level and the job
  self.assertIn('python3 scripts/deploy_paths.py HEAD FETCH_HEAD',WORKFLOW)  # the stale-run guard stays, on a source digest
  self.assertIn("github.ref == 'refs/heads/main'",WORKFLOW)
 def test_push_of_ui_or_source_paths_runs_validate_current(self):
  on=WORKFLOW.split('permissions:',1)[0]
  self.assertRegex(on,r'\n  push:\n    branches: \[main\]')
  paths=re.search(r'    paths:\n((?:      - .*\n)+)',on)[1].split()
  for name in ('index.html','app.js','style.css','scripts/**','trending/**','docs/agent-feed.md','llms.txt'):self.assertIn(name,paths)
  self.assertNotIn('tests/**',paths)
  self.assertIn("MODE: ${{ inputs.mode || (github.event_name == 'push' && 'validate-current') || 'refresh' }}",WORKFLOW)
  self.assertIn("    - cron: '23 7 * * *'",WORKFLOW)
 def test_previous_publication_is_the_retained_pages_artifact(self):
  job=WORKFLOW.split('  refresh-and-publish:',1)[1]
  self.assertIn('      actions: write  #',job)  # list runs and download their github-pages artifact; dispatch a replacement refresh
  self.assertRegex(job,r'uses: actions/upload-pages-artifact@v3\n        with:\n          path: \$\{\{ runner.temp \}\}/site\n(?:          #.*\n)?          retention-days: 30\n')
  self.assertIn("BOOTSTRAP: ${{ inputs.bootstrap_from_live && '--bootstrap-from-live' || '' }}",job);self.assertIn('$BOOTSTRAP',job)
  self.assertRegex(WORKFLOW,r'      bootstrap_from_live:\n        description: .*\n        type: boolean\n        default: false\n')
  self.assertNotIn('--published-site',WORKFLOW)  # Actions runs never read the CDN unless bootstrapping
 def test_push_filter_and_deploy_guard_share_one_path_list(self):
  on=WORKFLOW.split('permissions:',1)[0];paths=re.search(r'    paths:\n((?:      - .*\n)+)',on)[1].split()[1::2]
  self.assertEqual(tuple(paths),DEPLOY_PATHS)
  self.assertIn('cancel-in-progress: false',WORKFLOW)
 def test_deploy_guard_ignores_changes_outside_the_deploy_paths(self):
  # Codex full-lists review #4: a tests-only commit landing on main while a refresh runs triggers no new run, so the old exact
  # "live main == HEAD" guard threw the day's refresh away. The guard step runs here as written in the workflow.
  step=re.search(r'      - name: Refuse to deploy a stale run\n        env:\n(?:          .*\n)+        run: \|\n((?:          .*\n)+)',WORKFLOW)[1]
  with tempfile.TemporaryDirectory() as tmp:
   tmp=pathlib.Path(tmp);git=lambda *a,cwd=tmp/'work':subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',*a],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()
   subprocess.run(['git','init','-q','--bare','-b','main',str(tmp/'origin.git')],check=True);(tmp/'work').mkdir();git('init','-q','-b','main')
   for name in ('app.js','tests/test_x.py','scripts/deploy_paths.py','.github/workflows/weekly-refresh.yml'):
    (tmp/'work'/name).parent.mkdir(parents=True,exist_ok=True);(tmp/'work'/name).write_bytes((ROOT/name).read_bytes() if (ROOT/name).exists() else b'x')
   git('add','-A');git('commit','-qm','run');run=git('rev-parse','HEAD');git('remote','add','origin',(tmp/'origin.git').as_uri())
   (tmp/'bin').mkdir();(tmp/'bin'/'gh').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_LOG"\n');(tmp/'bin'/'gh').chmod(0o755)  # a stub gh: records its arguments
   env=lambda mode:dict(os.environ,PATH=f"{tmp/'bin'}:{os.environ['PATH']}",GH_LOG=str(tmp/'gh.log'),MODE=mode,GITHUB_SERVER_URL='https://github.com',GITHUB_REPOSITORY='o/r')
   guard=lambda mode='refresh':subprocess.run(['bash','-e','-c',step],cwd=tmp/'work',capture_output=True,text=True,env=env(mode))
   gh_calls=lambda:(tmp/'gh.log').read_text().splitlines() if (tmp/'gh.log').exists() else []
   def land(name,text):
    git('checkout','-q','main');(tmp/'work'/name).write_text(text);git('commit','-qam',name);git('push','-q','origin','main');git('checkout','-q','--detach',run)
   land('tests/test_x.py','y');proc=guard();self.assertEqual(proc.returncode,0,proc.stderr);self.assertIn('deploy-relevant sources match live main',proc.stdout)
   self.assertEqual(gh_calls(),[])
   land('app.js','changed');proc=guard('validate-current');self.assertEqual(proc.returncode,1);self.assertIn('newer UI/scripts on main',proc.stderr);self.assertEqual(gh_calls(),[])
 def test_a_refresh_superseded_by_a_source_change_dispatches_a_replacement_refresh(self):
  # Codex full-lists review 2 #3: the queued push run only re-releases, so a refresh stopped by the guard lost its fresh collection.
  step=re.search(r'      - name: Refuse to deploy a stale run\n        env:\n((?:          .*\n)+)        run: \|\n((?:          .*\n)+)',WORKFLOW)
  self.assertIn("GH_TOKEN: ${{ github.token }}",step[1]);self.assertIn("MODE: ${{ inputs.mode || (github.event_name == 'push' && 'validate-current') || 'refresh' }}",step[1])
  with tempfile.TemporaryDirectory() as tmp:
   tmp=pathlib.Path(tmp);git=lambda *a,cwd=tmp/'work':subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',*a],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()
   subprocess.run(['git','init','-q','--bare','-b','main',str(tmp/'origin.git')],check=True);(tmp/'work').mkdir();git('init','-q','-b','main')
   for name in ('app.js','scripts/deploy_paths.py','.github/workflows/weekly-refresh.yml'):
    (tmp/'work'/name).parent.mkdir(parents=True,exist_ok=True);(tmp/'work'/name).write_bytes((ROOT/name).read_bytes())
   git('add','-A');git('commit','-qm','run');run=git('rev-parse','HEAD');git('remote','add','origin',(tmp/'origin.git').as_uri())
   (tmp/'work'/'app.js').write_text('changed');git('commit','-qam','ui');git('push','-q','origin','main');git('checkout','-q','--detach',run)
   (tmp/'bin').mkdir();(tmp/'bin'/'gh').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_LOG"\n');(tmp/'bin'/'gh').chmod(0o755)
   proc=subprocess.run(['bash','-e','-c',step[2]],cwd=tmp/'work',capture_output=True,text=True,
                       env=dict(os.environ,PATH=f"{tmp/'bin'}:{os.environ['PATH']}",GH_LOG=str(tmp/'gh.log'),MODE='refresh',GITHUB_SERVER_URL='https://github.com',GITHUB_REPOSITORY='o/r'))
   self.assertEqual(proc.returncode,1);self.assertEqual((tmp/'gh.log').read_text().splitlines(),['workflow run weekly-refresh.yml --ref main -f mode=refresh'])
   self.assertIn('sources changed on main; replacement refresh https://github.com/o/r/actions/workflows/weekly-refresh.yml dispatched',proc.stderr)
  # Nothing is uploaded or deployed after the guard fails: both steps follow it and run only on success (no `if:`).
  job=WORKFLOW.split('    steps:\n',1)[1];names=re.findall(r'      - name: (.*)\n',job)
  guard,upload,deploy=(names.index(n) for n in ('Refuse to deploy a stale run','Upload exact staged Pages artifact','Deploy exact artifact'))
  self.assertLess(guard,upload);self.assertLess(upload,deploy)
  for name in ('Upload exact staged Pages artifact','Deploy exact artifact'):
   self.assertNotIn('        if:',job.split(f'      - name: {name}\n',1)[1].split('      - name:',1)[0])
 def test_a_re_run_is_refused_before_anything_else(self):
  job=WORKFLOW.split('    steps:\n',1)[1]
  self.assertTrue(job.startswith('      - name: Refuse a re-run\n'))
  script=re.search(r'      - name: Refuse a re-run\n        run: \|\n((?:          .*\n)+)',job)[1]
  for attempt,code in (('1',0),('2',1)):
   proc=subprocess.run(['bash','-e','-c',script],env={'GITHUB_RUN_ATTEMPT':attempt,'PATH':'/usr/bin:/bin'},capture_output=True,text=True)
   self.assertEqual(proc.returncode,code)
  self.assertIn('re-runs are disabled; dispatch a new run',proc.stderr)
 def test_a_partial_refresh_is_deployed_and_then_fails_the_run(self):
  job=WORKFLOW.split('  refresh-and-publish:',1)[1];steps=re.findall(r'      - (?:name: (.*)|uses: (.*))\n',job);names=[n or u for n,u in steps]
  stage=job.split('id: stage',1)[1].split('      - name:',1)[0]
  self.assertIn('set +e',stage);self.assertIn('echo "code=$code" >> "$GITHUB_OUTPUT"',stage);self.assertTrue(stage.rstrip().endswith('[ "$code" = 0 ] || [ "$code" = 3 ]'))
  self.assertEqual(names[-2:],['Deploy exact artifact','Fail the run when a window kept its previous rows'])  # after the deploy, never before
  self.assertIn("        if: steps.stage.outputs.code == '3'\n",job.split('Fail the run when a window kept its previous rows',1)[1])
 def test_depth_and_verify_top_reach_the_script_and_the_run_may_take_six_hours(self):
  self.assertIn('timeout-minutes: 360',WORKFLOW)
  self.assertIn('--depth "$DEPTH" --verify-top "$VERIFY_TOP" ${TOP:+--top "$TOP"}',WORKFLOW)
  self.assertIn("DEPTH: day=${{ inputs.day_depth || 'all' }},week=${{ inputs.week_depth || 'all' }},month=${{ inputs.month_depth || '20000' }}",WORKFLOW)
  for name,default in (('day_depth','all'),('week_depth','all'),('month_depth','20000')):self.assertRegex(WORKFLOW,rf"      {name}:\n        description: .*\n        type: string\n        default: '{default}'\n")
  self.assertNotRegex(WORKFLOW,r'\n      depth:\n')
  self.assertIn('--deadline-minutes 330',WORKFLOW)  # 30 min under timeout-minutes: 360 for the date fill and the deploy
  self.assertIn("VERIFY_TOP: ${{ inputs.verify_top || '2000' }}",WORKFLOW)
  self.assertRegex(WORKFLOW,r"      top:\n        description: .*\n        type: string\n        default: ''\n")  # legacy top input is empty by default
  self.assertIn('python3 -m unittest discover -s tests',WORKFLOW)
