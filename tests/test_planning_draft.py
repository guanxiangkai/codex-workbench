import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from codex_workbench.planning_draft import PlanningDraft
class T(unittest.TestCase):
 def models(self): return [{'id':'glm-1','provider_id':'bigmodel','model_type':'reasoning','version':'1','validation_status':'verified'},{'id':'mm-2','provider_id':'minimax','model_type':'reasoning','version':'2','validation_status':'verified'}]
 def test_normal_and_minimax_first(self):
  seen=[]
  p=PlanningDraft(self.models,lambda m,t,timeout:(seen.append(m['provider_id']) or {'text':'{"project":"P","section":"S","tasks":[{"title":"T","prompt":"Do","tags":["x"]}]}'}))
  x=p.generate('安排工作');self.assertEqual('model',x['source']);self.assertEqual('minimax',seen[0])
 def test_model_error_manual(self):
  x=PlanningDraft(self.models,lambda *a:(_ for _ in ()).throw(RuntimeError())).generate('笔记内容');self.assertEqual('manual',x['source']);self.assertEqual('model_failed',x['warning'])
 def test_completed_thinking_is_not_part_of_editable_draft(self):
  raw='<think>private reasoning</think>\n```json\n{"tasks":[{"title":"计划","prompt":"整理资料"}]}\n```'
  x=PlanningDraft(self.models,lambda *a:{'text':raw}).generate('整理资料')
  self.assertEqual('model',x['source']);self.assertEqual('计划',x['tasks'][0]['title']);self.assertNotIn('private reasoning',str(x))
 def test_malicious_overflow_rejected(self):
  raw='{"tasks":'+str([{'title':'x','prompt':'y','tags':[]} for _ in range(13)])+'}'
  x=PlanningDraft(self.models,lambda *a:{'text':raw}).generate('原文');self.assertEqual('manual',x['source'])
 def test_dates_are_preserved_but_not_invented(self):
  raw='{"tasks":[{"title":"计划","prompt":"整理","start_date":"2026-09-01","due_date":"2026-09-30"}]}'
  x=PlanningDraft(self.models,lambda *a:{'text':raw}).generate('原文')
  self.assertEqual(x['tasks'][0]['start_date'],'2026-09-01')
  self.assertEqual(x['tasks'][0]['due_date'],'2026-09-30')
  self.assertIsNone(PlanningDraft(self.models,lambda *a:{'text':'{"tasks":[{"title":"计划","prompt":"整理"}]}'}).generate('原文')['tasks'][0]['start_date'])
 def test_invalid_draft_dates_fall_back_to_manual(self):
  raw='{"tasks":[{"title":"计划","prompt":"整理","start_date":"2026-02-30"}]}'
  self.assertEqual(PlanningDraft(self.models,lambda *a:{'text':raw}).generate('原文')['source'],'manual')
if __name__=='__main__':unittest.main()
