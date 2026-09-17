/* 单条配置的浏览器端加密通道。私钥仅留在本次请求内，不写 URL 或持久存储。 */
const ConfigurationCrypto=(()=>{
  const encode=bytes=>{let value='';for(let i=0;i<bytes.length;i+=8192)value+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(value)};
  const decode=(value,maximum)=>{if(typeof value!=='string'||value.length>maximum||value.length%4||!value||!/^[A-Za-z0-9+/]+={0,2}$/.test(value))throw new Error('配置加密响应无效');return Uint8Array.from(atob(value),c=>c.charCodeAt(0))};
  const hex=bytes=>[...new Uint8Array(bytes)].map(v=>v.toString(16).padStart(2,'0')).join('');
  async function prepare(entry){
    if(!entry||typeof entry.id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(entry.id))throw new Error('请选择有效配置');
    const keys=await crypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},false,['encrypt','decrypt']);
    const publicBytes=new Uint8Array(await crypto.subtle.exportKey('spki',keys.publicKey));
    const request_id=crypto.randomUUID().replace(/-/g,'');
    return {arguments:{id:entry.id,public_key:encode(publicBytes),request_id},digest:hex(await crypto.subtle.digest('SHA-256',publicBytes)),revision:entry.revision,privateKey:keys.privateKey};
  }
  async function decrypt(request,response){
    let keyBytes,plainBytes;
    try{
    const expected=['entry_id','revision','generation','request_id','public_key_sha256','wrapped_key','iv','ciphertext'];
    if(!request?.privateKey||!response||Object.keys(response).length!==expected.length||expected.some(k=>!Object.prototype.hasOwnProperty.call(response,k)))throw new Error('配置加密响应无效');
    if(response.entry_id!==request.arguments.id||response.request_id!==request.arguments.request_id||response.public_key_sha256!==request.digest||!Number.isSafeInteger(response.revision)||response.revision<1||!Number.isSafeInteger(response.generation)||response.generation<0)throw new Error('配置加密响应不匹配');
    if(Number.isSafeInteger(request.revision)&&request.revision!==response.revision)throw new Error('配置已更新，请重新打开');
    const wrapped=decode(response.wrapped_key,1024),iv=decode(response.iv,64),ciphertext=decode(response.ciphertext,2*1024*1024);
    if(wrapped.length!==256||iv.length!==12||ciphertext.length<16)throw new Error('配置加密响应无效');
      keyBytes=new Uint8Array(await crypto.subtle.decrypt({name:'RSA-OAEP'},request.privateKey,wrapped));
      if(keyBytes.length!==32)throw new Error('配置加密响应无效');
      const aes=await crypto.subtle.importKey('raw',keyBytes,{name:'AES-GCM'},false,['decrypt']);
      const aad=new TextEncoder().encode(JSON.stringify({entry_id:response.entry_id,public_key_sha256:response.public_key_sha256,request_id:response.request_id,revision:response.revision}));
      plainBytes=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv,additionalData:aad},aes,ciphertext));
      const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(plainBytes));
      if(!value||typeof value.format!=='string'||!['workbench_credential_v1','workbench_credential','workbench_model_key','json','text'].includes(value.format))throw new Error('配置内容格式无效');
      if(value.format==='text'&&typeof value.text!=='string')throw new Error('配置内容格式无效');
      if(value.format!=='text'&&!Object.prototype.hasOwnProperty.call(value,'fields'))throw new Error('配置内容格式无效');
      return value;
    }catch(error){throw new Error(error.message.startsWith('配置')?error.message:'无法解密配置，请重新查看')}
    finally{keyBytes?.fill(0);plainBytes?.fill(0);if(request&&typeof request==='object')request.privateKey=null;}
  }
  return Object.freeze({prepare,decrypt});
})();
