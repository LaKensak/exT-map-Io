import struct, sys, time
sys.path.insert(0, r'F:\raid\pythonProject4\tools\rust_esp_mvc')
import auto_offsets as a

p = r'F:\SteamLibrary\steamapps\common\Rust\GameAssembly.dll'
with open(p,'rb') as f:
    header = f.read(0x1000)
ib, sects = a._parse_pe_sections(header)
code = next(s for s in sects if s['name'] == 'il2cpp')
data = [s for s in sects if s['name'] in ('.data', '.rdata')]
lo = min(s['va'] for s in data); hi = max(s['va']+s['raw_sz'] for s in data)
print('code', hex(code['va']), hex(code['raw_sz']), 'data', hex(lo), hex(hi))
found = set()
t = time.time()
with open(p,'rb') as f:
    f.seek(code['fo']); rem = code['raw_sz']; va = code['va']
    while rem > 0:
        chunk = f.read(min(0x400000, rem))
        if not chunk: break
        found.update(a._scan_code_for_data_refs(chunk, va, lo, hi))
        va += len(chunk); rem -= len(chunk)
print('candidates', len(found), 'in %.1fs' % (time.time()-t))
for n,v in (('MainCamera',0x119B0A40),('BasePlayer',0x11999730),('BaseNetworkable',0x119B5410),('LCPM',0x1196D5A8)):
    print(n, hex(v), v in found)
import pickle
pickle.dump(found, open('cand_il2cpp.pkl','wb'))
