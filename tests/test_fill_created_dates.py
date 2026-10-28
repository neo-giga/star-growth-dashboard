import importlib.util,json,pathlib,subprocess,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('fill_created_dates',ROOT/'scripts/fill_created_dates.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def row(name,rid,created=None,created_id=None,meta_id=None):
 r={'repo_id':rid,'full_name':name,'stars':1,'gain':1,'language':None,'created_at':created,'created_at_github_repo_id':created_id}
 if meta_id is not None: r['metadata']={'github_repo_id':meta_id}
 return r
def feed(period,rows):
 return {'provider':'gittrend','period':period,'rows':rows,'creation_dates':{'known':0,'unknown':0}}
class MergeTests(unittest.TestCase):
 def test_no_id_accepts_date_and_records_github_id(self):
  r=row('o/a',1);self.assertEqual(m.merge_created_dates([r],{'o/a':(55,'2024-01-02T03:04:05Z')}),1)
  self.assertEqual(r['created_at'],'2024-01-02T03:04:05+00:00');self.assertEqual(r['created_at_github_repo_id'],55)
 def test_matching_id_accepts_mismatch_stays_blank(self):
  ok=row('o/a',1,created_id=55);bad=row('o/b',2,created_id=56);meta=row('o/c',3,meta_id=57)
  found={'o/a':(55,'2024-01-02T03:04:05Z'),'o/b':(99,'2024-01-02T03:04:05Z'),'o/c':(99,'2024-01-02T03:04:05Z')}
  self.assertEqual(m.merge_created_dates([ok,bad,meta],found),1)
  self.assertEqual(ok['created_at'],'2024-01-02T03:04:05+00:00');self.assertIsNone(bad['created_at']);self.assertEqual(bad['created_at_github_repo_id'],56);self.assertIsNone(meta['created_at'])
 def test_existing_date_and_unknown_names_untouched(self):
  dated=row('o/a',1,created='2020-01-01T00:00:00+00:00',created_id=5);missing=row('o/z',2)
  self.assertEqual(m.merge_created_dates([dated,missing],{'o/a':(5,'2024-01-02T03:04:05Z'),'o/b':(6,'2024-01-02T03:04:05Z')}),0)
  self.assertEqual(dated['created_at'],'2020-01-01T00:00:00+00:00');self.assertIsNone(missing['created_at'])
 def test_name_match_is_case_insensitive(self):
  r=row('O/A',1);self.assertEqual(m.merge_created_dates([r],{'o/a':(5,'2024-01-02T03:04:05Z')}),1)
 def test_invalid_timestamp_rejected(self):
  r=row('o/a',1);self.assertEqual(m.merge_created_dates([r],{'o/a':(5,'not a date')}),0);self.assertIsNone(r['created_at'])
class FillTests(unittest.TestCase):
 def test_batches_dedupe_names_and_update_counts(self):
  calls=[]
  def query(names):
   calls.append(list(names));return {n.casefold():(i+1,'2024-01-02T03:04:05Z') for i,n in enumerate(names)}
  rows=[row(f'o/r{i}',i+1) for i in range(150)]
  data={'feeds':[feed('day',[row(f'o/r{i}',i+1) for i in range(3)]),feed('week',rows+[row('o/dated',999,created='2020-01-01T00:00:00+00:00',created_id=1)])]}
  summary=m.fill_created_dates(data,query=query,batch_size=100)
  self.assertEqual([len(c) for c in calls],[100,50]);self.assertEqual(len(set(sum(calls,[]))),150)
  self.assertEqual(summary['requested'],150);self.assertEqual(summary['filled'],153);self.assertEqual(summary['failed_batches'],0)
  self.assertTrue(all(r['created_at'] for r in rows));self.assertEqual(data['feeds'][1]['creation_dates'],{'known':151,'unknown':0});self.assertEqual(data['feeds'][0]['creation_dates'],{'known':3,'unknown':0})
 def test_failed_batch_is_skipped_not_fatal(self):
  def query(names):
   if names[0]=='o/r0': raise RuntimeError('gh: boom')
   return {n.casefold():(1,'2024-01-02T03:04:05Z') for n in names}
  rows=[row(f'o/r{i}',i+1) for i in range(3)]
  summary=m.fill_created_dates({'feeds':[feed('week',rows)]},query=query,batch_size=2)
  self.assertEqual(summary['failed_batches'],1);self.assertEqual(summary['filled'],1);self.assertEqual(summary['errors'],['gh: boom']);self.assertIsNone(rows[0]['created_at']);self.assertEqual(rows[2]['created_at'],'2024-01-02T03:04:05+00:00')
 def test_nothing_missing_makes_no_query(self):
  summary=m.fill_created_dates({'feeds':[feed('week',[row('o/a',1,created='2020-01-01T00:00:00+00:00')])]},query=lambda names:self.fail('queried'))
  self.assertEqual(summary,{'requested':0,'filled':0,'batches':0,'failed_batches':0,'errors':[]})
class GraphQLTests(unittest.TestCase):
 def test_query_uses_gh_and_tolerates_partial_errors(self):
  out={'data':{'r0':{'databaseId':12,'createdAt':'2024-01-02T03:04:05Z'},'r1':None,'r2':{'databaseId':None,'createdAt':'2024-01-02T03:04:05Z'}},'errors':[{'type':'NOT_FOUND','path':['r1']}]}
  with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess(['gh'],1,json.dumps(out),'gh: Could not resolve')) as run:
   found=m.query_created_dates(['o/a','o/b','o/c'])
  self.assertEqual(found,{'o/a':(12,'2024-01-02T03:04:05Z')})
  args,kwargs=run.call_args;self.assertEqual(args[0],['gh','api','graphql','--input','-']);body=json.loads(kwargs['input'])
  self.assertIn('r0: repository(owner:"o", name:"a") { databaseId createdAt }',body['query']);self.assertIn('r2: repository(owner:"o", name:"c")',body['query'])
 def test_non_json_output_raises(self):
  with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess(['gh'],4,'','gh: not logged in')):
   with self.assertRaises(RuntimeError): m.query_created_dates(['o/a'])
 def test_unsafe_name_rejected(self):
  with self.assertRaises(ValueError): m.query_created_dates(['o/a"){x}'])
