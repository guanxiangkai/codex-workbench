import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
const scope={crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,console,btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary')};
runInNewContext(readFileSync(new URL('../ui/configuration-crypto.js',import.meta.url),'utf8')+'\nthis.api=ConfigurationCrypto;',scope);
const api=scope.api;
async function response(request,value={format:'workbench_credential_v1',fields:{credential:{password:'synthetic-only',port:5432}}}){
 const r={entry_id:request.arguments.id,revision:4,generation:2,request_id:request.arguments.request_id,public_key_sha256:request.digest};
 const publicKey=await webcrypto.subtle.importKey('spki',Buffer.from(request.arguments.public_key,'base64'),{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
 const aes=await webcrypto.subtle.generateKey({name:'AES-GCM',length:256},true,['encrypt']);const raw=await webcrypto.subtle.exportKey('raw',aes),iv=webcrypto.getRandomValues(new Uint8Array(12));
 const aad=new TextEncoder().encode(JSON.stringify({entry_id:r.entry_id,public_key_sha256:r.public_key_sha256,request_id:r.request_id,revision:r.revision}));
 r.wrapped_key=Buffer.from(await webcrypto.subtle.encrypt({name:'RSA-OAEP'},publicKey,raw)).toString('base64');r.iv=Buffer.from(iv).toString('base64');r.ciphertext=Buffer.from(await webcrypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad},aes,new TextEncoder().encode(JSON.stringify(value)))).toString('base64');return r;
}
const request=await api.prepare({id:'entry-1',revision:4});assert.equal(request.privateKey.extractable,false);assert.deepEqual(Object.keys(request.arguments).sort(),['id','public_key','request_id']);const encrypted=await response(request);assert.equal(JSON.stringify(encrypted).includes('synthetic-only'),false);const decoded=await api.decrypt(request,encrypted);assert.equal(decoded.fields.credential.password,'synthetic-only');assert.equal(decoded.fields.credential.port,5432);assert.equal(request.privateKey,null);await assert.rejects(()=>api.decrypt(request,encrypted));
for(const mutate of [r=>({...r,entry_id:'entry-2'}),r=>({...r,request_id:'different-request-id'}),r=>({...r,revision:5}),r=>({...r,public_key_sha256:'0'.repeat(64)}),r=>({...r,generation:true}),r=>({...r,ciphertext:r.ciphertext.slice(0,-4)+'AAAA'})]){const req=await api.prepare({id:'entry-1',revision:4});await assert.rejects(async()=>api.decrypt(req,mutate(await response(req))));assert.equal(req.privateKey,null);}
const raw=await api.prepare({id:'entry-1'});assert.equal((await api.decrypt(raw,await response(raw,{format:'text',text:'synthetic\nmultiline'}))).text,'synthetic\nmultiline');
console.log('Configuration encryption round trip, request binding, tampering and private-key disposal passed');
// 浏览器公钥 → Python 固定消费者 → 浏览器解密，验证跨语言 AAD 一致。
const {spawnSync}=await import('node:child_process');
const {fileURLToPath}=await import('node:url');
const path=await import('node:path');
const root=fileURLToPath(new URL('..',import.meta.url));
const cross=await api.prepare({id:'fixture-entry',revision:4});
const synthetic={schema:'codex-workbench.credential',version:1,credential:{private_key:'synthetic-pgp\nline-two',account:'fixture-user',port:5432}};
const processResult=spawnSync(path.join(process.env.HOME,'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'),['-B','-m','codex_workbench.credential_details',cross.arguments.id,'4','2',cross.arguments.request_id,cross.arguments.public_key],{cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'src')},input:JSON.stringify(synthetic),encoding:'utf8',timeout:10000});
assert.equal(processResult.status,0);assert.equal(processResult.stderr,'');assert.equal(processResult.stdout.includes('synthetic-pgp'),false);
const crossDecoded=await api.decrypt(cross,JSON.parse(processResult.stdout));assert.equal(crossDecoded.fields.credential.private_key,'synthetic-pgp\nline-two');assert.equal(crossDecoded.fields.credential.port,5432);
console.log('Python consumer to browser encrypted-detail interoperability passed');
