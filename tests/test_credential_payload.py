import base64,json,os,shlex,tempfile,unittest
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from codex_workbench.credential_payload import CredentialPayloadSessions,CredentialPayloadVault

class PayloadTest(unittest.TestCase):
 def test_utf8_payload_and_fake_vault(self):
  context={'name':'标题','model_type':'credential','base_url':'https://x.invalid','model_id':None,'version':None}; sessions=CredentialPayloadSessions(); prepared=sessions.prepare(context); public=serialization.load_der_public_key(base64.b64decode(prepared['public_key'])); aes=os.urandom(32); iv=os.urandom(12); raw=json.dumps({'description':'中文说明','account':'u','password':'p','port':443},ensure_ascii=False,separators=(',',':')).encode(); wrapped=public.encrypt(aes,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None)); envelope={'session_id':prepared['id'],'wrapped_key':base64.b64encode(wrapped).decode(),'iv':base64.b64encode(iv).decode(),'ciphertext':base64.b64encode(AESGCM(aes).encrypt(iv,raw,prepared['id'].encode())).decode()}; payload=sessions.consume_payload(envelope,context); self.assertEqual('中文说明',payload['description'])
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); out=root/'out'; script=root/'v.sh'; script.write_text('#!/bin/sh\nif [ "$1" = put-stdin ]; then cat > '+shlex.quote(str(out))+'; exit 0; fi\n[ "$1" = verify ] && exit 0\nexit 1\n'); script.chmod(0o700); ref=CredentialPayloadVault(script).store(payload,context); self.assertTrue(ref.startswith('vault:credential-')); self.assertNotIn('中文说明', ' '.join([str(script),ref])); self.assertEqual(payload,json.loads(out.read_text())['credential'])
if __name__=='__main__':unittest.main()
