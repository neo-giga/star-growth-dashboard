"""The installed workflow: no git writes, one artifact per run, a push to main of UI/source files re-publishes with validate-current."""
import importlib.util,pathlib,re,subprocess,tempfile,unittest
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
  self.assertIn('      actions: read\n',job)  # list runs and download their github-pages artifact with the job token
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
  step=re.search(r'      - name: Refuse to deploy a stale run\n        run: \|\n((?:          .*\n)+)',WORKFLOW)[1]
  with tempfile.TemporaryDirectory() as tmp:
   tmp=pathlib.Path(tmp);git=lambda *a,cwd=tmp/'work':subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',*a],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()
   subprocess.run(['git','init','-q','--bare','-b','main',str(tmp/'origin.git')],check=True);(tmp/'work').mkdir();git('init','-q','-b','main')
   for name in ('app.js','tests/test_x.py','scripts/deploy_paths.py','.github/workflows/weekly-refresh.yml'):
    (tmp/'work'/name).parent.mkdir(parents=True,exist_ok=True);(tmp/'work'/name).write_bytes((ROOT/name).read_bytes() if (ROOT/name).exists() else b'x')
   git('add','-A');git('commit','-qm','run');run=git('rev-parse','HEAD');git('remote','add','origin',(tmp/'origin.git').as_uri())
   guard=lambda:subprocess.run(['bash','-e','-c',step],cwd=tmp/'work',capture_output=True,text=True)
   def land(name,text):
    git('checkout','-q','main');(tmp/'work'/name).write_text(text);git('commit','-qam',name);git('push','-q','origin','main');git('checkout','-q','--detach',run)
   land('tests/test_x.py','y');proc=guard();self.assertEqual(proc.returncode,0,proc.stderr);self.assertIn('deploy-relevant sources match live main',proc.stdout)
   land('app.js','changed');proc=guard();self.assertEqual(proc.returncode,1);self.assertIn('newer UI/scripts on main',proc.stderr);self.assertIn('a new run is queued',proc.stderr)
 def test_depth_and_verify_top_reach_the_script_and_the_run_may_take_six_hours(self):
  self.assertIn('timeout-minutes: 360',WORKFLOW)
  self.assertIn('--depth "$DEPTH" --verify-top "$VERIFY_TOP" ${TOP:+--top "$TOP"}',WORKFLOW)
  self.assertIn("DEPTH: ${{ inputs.depth || 'all' }}",WORKFLOW);self.assertIn("VERIFY_TOP: ${{ inputs.verify_top || '2000' }}",WORKFLOW)
  self.assertRegex(WORKFLOW,r"      top:\n        description: .*\n        type: string\n        default: ''\n")  # legacy top input is empty by default
  self.assertIn('python3 -m unittest discover -s tests',WORKFLOW)
