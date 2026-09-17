"""业务集成测试的合成标题与非秘密凭证目录，不访问真实账户或保险库。"""
from codex_workbench.titles import suggest_title


def fake_title(prompt, account_home=None, **kwargs):
    return suggest_title(prompt)


class MemoryCredentials:
    def __init__(self):
        self.entries = {}
        self.folders = []

    def list(self):
        return {"entries": list(self.entries.values()), "folders": list(self.folders), "status": {"ready": True}}

    def register_key(self, reference, label, base_url, *, created):
        assert created is True
        identity = reference.removeprefix("vault:")
        entry = {"id": identity, "reference": reference, "label": label, "kind": "api_key", "base_urls": [base_url.rstrip("/")], "folder_id": None, "tags": [], "color": None}
        self.entries[identity] = entry
        return entry

    def resolve_key(self, identity, base_url):
        entry = self.entries.get(identity)
        if not entry or base_url.rstrip("/") not in entry["base_urls"]:
            raise ValueError("凭证目标不匹配")
        return entry["reference"]

    def metadata_export(self):
        return {"folders": [], "entries": []}

    def entry_update(self, identity, **fields):
        self.entries[identity].update(fields)
        return self.entries[identity]
