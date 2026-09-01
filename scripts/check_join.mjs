import fs from 'fs';
const path = 'photon-sniffer/captures/406fa397-1a8f-4a4d-8a0f-1234567890ab.jsonl';
const files = fs.readdirSync('photon-sniffer/captures').filter(f=>f.endsWith('.jsonl'));
let hex='';
for(const f of files){
  const txt=fs.readFileSync('photon-sniffer/captures/'+f,'utf8');
  for(const line of txt.split('\n')){
    if(!line.trim()) continue;
    const j=JSON.parse(line);
    if(j.rawHex && j.rawHex.startsWith('f302e2')){
      hex=j.rawHex;
      console.log(f, hex.slice(0,200));
      break;
    }
  }
  if(hex) break;
}
if(!hex) console.log('not found');
else {
  // decode via decryptor
  const dec = await import('./photon-sniffer/decryptor.js');
}
