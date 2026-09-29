"""Install the narrow HTTP CONNECT patch without enabling a proxy server."""
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]

def patch():
    utils=ROOT/'vendor/danmuapi/danmu_api/utils'
    shutil.copyfile(ROOT/'integrations/bahamut-proxy.js',utils/'animeservice-bahamut-proxy.js')
    p=utils/'http-util.js';s=p.read_text(encoding='utf-8')
    marker="import { bahamutProxyAgent } from './animeservice-bahamut-proxy.js';"
    if marker not in s:
        s=marker+'\n'+s
        old="      if (shouldUseNodeFetch()) {"
        assert old in s
        # GET only: search, episode metadata and comments. Leave all other sources unchanged.
        s=s.replace(old,"      const animeServiceProxy = bahamutProxyAgent(url);\n      if (animeServiceProxy || shouldUseNodeFetch()) {",1)
        s=s.replace('agent: nodeFetchAgent,',"agent: animeServiceProxy || nodeFetchAgent,",1)
        s=s.replace("redirect: allow_redirects ? 'follow' : 'manual'", "redirect: animeServiceProxy ? 'error' : (allow_redirects ? 'follow' : 'manual')",1)
        p.write_text(s,encoding='utf-8')
    # The pinned upstream supports Bahamut by cached ID but omits it from its
    # stable-URL endpoint. Do not fall through to an unrelated third-party server.
    p=ROOT/'vendor/danmuapi/danmu_api/apis/dandan-api.js'
    s=p.read_text(encoding='utf-8')
    marker='// AnimeService: stable Bahamut URL routing.'
    if marker not in s:
        before,after=s.split('export async function getCommentByUrl(',1)
        anchor="    if (url.includes('.qq.com')) {"
        assert anchor in after
        replacement='''    // AnimeService: stable Bahamut URL routing.
    const bahaUrl = cleanUrl.match(/^https:\\/\\/ani\\.gamer\\.com\\.tw\\/animeVideo\\.php\\?sn=([1-9][0-9]{0,11})$/);
    if (bahaUrl) {
      danmus = await sourceLogContext.run('bahamut', () => bahamutSource.getComments(bahaUrl[1], 'bahamut', segmentFlag));
    } else if (url.includes('.qq.com')) {'''
        after=after.replace(anchor,replacement,1)
        p.write_text(before+'export async function getCommentByUrl('+after,encoding='utf-8')

if __name__=='__main__':patch()
