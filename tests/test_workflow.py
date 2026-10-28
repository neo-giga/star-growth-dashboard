"""The installed workflow: no git writes, one artifact per run, a push to main of UI/source files re-publishes with validate-current."""
import pathlib,re,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
WORKFLOW=(ROOT/'.github/workflows/weekly-refresh.yml').read_text()
class WorkflowTests(unittest.TestCase):
 def test_docs_copy_is_byte_identical(self):
  self.assertEqual((ROOT/'docs/weekly-refresh.yml').read_bytes(),(ROOT/'.github/workflows/weekly-refresh.yml').read_bytes())
 def test_nothing_is_committed_or_pushed_and_contents_stay_read_only(self):
  for forbidden in ('git push','git commit','git add','contents: write'):self.assertNotIn(forbidden,WORKFLOW)
  self.assertEqual(WORKFLOW.count('contents: read'),2)  # top level and the job
  self.assertIn('git ls-remote origin refs/heads/main',WORKFLOW)  # the stale-run guard stays
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
 def test_depth_and_verify_top_reach_the_script_and_the_run_may_take_six_hours(self):
  self.assertIn('timeout-minutes: 360',WORKFLOW)
  self.assertIn('--depth "$DEPTH" --verify-top "$VERIFY_TOP" ${TOP:+--top "$TOP"}',WORKFLOW)
  self.assertIn("DEPTH: ${{ inputs.depth || 'all' }}",WORKFLOW);self.assertIn("VERIFY_TOP: ${{ inputs.verify_top || '2000' }}",WORKFLOW)
  self.assertRegex(WORKFLOW,r"      top:\n        description: .*\n        type: string\n        default: ''\n")  # legacy top input is empty by default
  self.assertIn('python3 -m unittest discover -s tests',WORKFLOW)
