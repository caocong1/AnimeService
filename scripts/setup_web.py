"""Reproduce the pinned, loopback-only danmu adapter; never modify media."""
from pathlib import Path
import shutil,subprocess
ROOT=Path(__file__).resolve().parents[1]
repo=ROOT/'vendor/danmuapi'
revision='28673ac764485b0f37966779bc3b14e2b97d31aa'
def run(args,cwd=ROOT):subprocess.run(args,cwd=cwd,check=True)
if not repo.exists():
    repo.parent.mkdir(parents=True,exist_ok=True)
    run(['git','clone','https://github.com/canmo101/danmuapi.git',str(repo)])
    run(['git','checkout',revision],repo)
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
if head!=revision:raise RuntimeError('Unexpected upstream revision; review local changes first')
p=repo/'danmu_api/server.js';s=p.read_text(encoding='utf-8')
if '  proxyServer = createProxyServer();' in s:
    a=s.index('  proxyServer = createProxyServer();');b=s.index('  //',a+1)
    s=s[:a]+'  // AnimeService: no general-purpose proxy listener.\n\n'+s[b:];p.write_text(s,encoding='utf-8')
p=repo/'danmu_api/utils/server-listen-util.js';s=p.read_text(encoding='utf-8').replace("host: '::'","host: '127.0.0.1'").replace("host: '0.0.0.0'","host: '127.0.0.1'");p.write_text(s,encoding='utf-8')
p=repo/'config/.env'
if not p.exists():p.write_text('DANMU_API_PORT=4872\nSOURCE_ORDER=bilibili,bahamut,iqiyi,youku,tencent\nRATE_LIMIT_MAX_REQUESTS=0\nSEARCH_CACHE_MINUTES=3\nCOMMENT_CACHE_MINUTES=3\nUSE_BANGUMI_DATA=false\n',encoding='utf-8')
npm=shutil.which('npm.cmd') or shutil.which('npm')
if not npm:raise RuntimeError('Node.js/npm is required')
run([npm,'ci','--ignore-scripts','--no-audit','--no-fund'],repo)
from patch_danmu_proxy import patch
patch()
from patch_danmu_bilibili import patch as patch_bilibili
patch_bilibili()
run([npm,'ci','--ignore-scripts','--no-audit','--no-fund'])
target=ROOT/'static/vendor';target.mkdir(exist_ok=True)
for package in ('artplayer','artplayer-plugin-danmuku'):
    shutil.copyfile(ROOT/f'node_modules/{package}/dist/{package}.js',target/f'{package}.js')
from setup_subtitles import install as install_subtitles
install_subtitles()
print('Web dependencies ready; no new subscription or download enabled.')
