import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from codex_workbench.planning_context import PlanningContext


class PlanningContextTest(unittest.TestCase):
    def test_scope_isolation_hash_change_and_asset_task_boundary(self):
        context = PlanningContext()
        output = context.retrieve(
            {"id": "task-1"},
            [{"scope": "project-a", "key": "a", "title": "保留", "content": "budget plan", "source": "catalog"},
             {"scope": "project-b", "key": "b", "title": "泄漏", "content": "secret"},
             {"scope": "project-a", "key": "changed", "title": "变更", "content": "new", "sha256": "old"}],
            [{"id": "same-task", "task_id": "task-1", "name": "evidence", "content": "proof"},
             {"id": "other-task", "task_id": "task-2", "name": "other", "content": "no"}],
            query="budget", allowed_scopes=["project-a"],
        )
        self.assertEqual(["a"], [item["id"] for item in output["items"]])
        self.assertEqual("catalog", output["items"][0]["provenance"]["source"])
        all_items = context.retrieve({"id": "task-1"}, [{"scope": "project-a", "key": "changed", "content": "new", "sha256": "old"}],
                                     [{"id": "same-task", "task_id": "task-1", "content": "proof"}], allowed_scopes=["project-a"])["items"]
        self.assertTrue(all_items[0]["hash_changed"])
        self.assertEqual(["changed", "same-task"], [item["id"] for item in all_items])

    def test_semantic_callbacks_require_verified_binding_and_fallback(self):
        called = []
        context = PlanningContext(embedding=lambda texts: called.append(texts) or {"model_id": "wrong", "dimensions": 2, "vectors": [[1, 0]] * len(texts)},
                                  embedding_binding={"model_id": "registered", "dimensions": 2})
        result = context.retrieve({"id": "t"}, [{"scope": "s", "key": "k", "content": "alpha"}],
                                  allowed_scopes=["s"], semantic=True)
        self.assertEqual("keyword", result["fallback"])
        self.assertFalse(result["used_embedding"])
        self.assertEqual([["alpha"]], called)

    def test_verified_delivery_only_creates_unreviewed_candidates(self):
        context = PlanningContext()
        delivery = {"verified": True, "knowledge_candidates": [{"title": "发现", "content": "已验证产物"}],
                    "accepted_evidence": {"accepted": True, "source": {"id": "run-1"}, "card_revision": 1,
                                          "card_sha256": "card", "verified_at": "2026-09-29T00:00:00+00:00"}}
        self.assertEqual([], context.extract_candidates(delivery, scope="missing", source={"id": "run-1"}, scope_exists=lambda value: False))
        result = context.extract_candidates(delivery, scope="known", source={"id": "run-1"}, scope_exists=lambda value: value == "known",
                                            relation_type="supports", direction="outbound")
        self.assertEqual("candidate", result[0]["status"])
        self.assertFalse(result[0]["reviewed"])
        self.assertEqual("supports", result[0]["relation_type"])
        self.assertTrue(result[0]["local_only"])
        self.assertEqual({"status": "unknown", "reason": "pending_confirmation"}, result[0]["applicability"])

    def test_semantic_finds_nonkeyword_match_caches_changes_and_excludes_private(self):
        calls=[]
        def embed(texts):
            calls.append(texts)
            return {'model_id':'e','vectors':[[1,0] for text in texts]}
        context=PlanningContext(embedding=embed,embedding_binding={'model_id':'e','dimensions':2},
            rerank=lambda query,items:{'model_id':'r','results':[{'index':i} for i in range(len(items))]},rerank_binding={'model_id':'r'})
        records=[{'scope':'s','key':'a','content':'approved evidence'},
                 {'scope':'s','key':'p','content':'private evidence','sensitivity':'private'},
                 {'scope':'s','key':'old','content':'stale','expires_at':'2000-01-01'}]
        result=context.retrieve({'id':'t'},records,query='验收',allowed_scopes=['s'],semantic=True)
        self.assertEqual(['a'],[item['id'] for item in result['items']])
        self.assertEqual(1,result['external_excluded'])
        self.assertEqual('old',result['expired_sources'][0]['id'])
        repeated=context.retrieve({'id':'t'},records,query='验收',allowed_scopes=['s'],semantic=True)
        self.assertEqual(1,repeated['cache_hits'])
        self.assertEqual(1,len(calls))
        records[0]['content']='changed evidence'
        context.retrieve({'id':'t'},records,query='验收',allowed_scopes=['s'],semantic=True)
        self.assertEqual(2,len(calls))
        self.assertNotIn('private',str(calls))

    def test_explicit_layers_keep_rules_skills_and_temporary_context_separate(self):
        context=PlanningContext()
        self.addCleanup(context.close)
        records=[{'scope':'s','key':layer,'content':layer,'source_layer':layer} for layer in ('rule','skill','stable_knowledge')]
        result=context.retrieve({'id':'t'},records,[{'id':'asset','task_id':'t','content':'temporary'}],allowed_scopes=['s'])
        self.assertEqual({'rule':['rule'],'skill':['skill'],'stable_knowledge':['stable_knowledge'],'task_context':['asset']},result['layers'])


if __name__ == "__main__":
    unittest.main()
