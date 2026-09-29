// AnimeService: only Bahamut's API uses the explicitly configured local proxy.
// Copied into the pinned adapter by scripts/setup_web.py.
import { HttpsProxyAgent } from 'https-proxy-agent';
let cachedUrl, cachedAgent;
export function bahamutProxyAgent(target, configured = process.env.ANIMESERVICE_BAHAMUT_PROXY || '') {
  if (new URL(target).hostname !== 'api.gamer.com.tw' || !configured) return null;
  const proxy = new URL(configured);
  if (proxy.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(proxy.hostname)
      || proxy.username || proxy.password || proxy.pathname !== '/' || proxy.search || proxy.hash) {
    throw new Error('Bahamut proxy must be a local HTTP endpoint without credentials');
  }
  if (cachedUrl !== proxy.href) {
    cachedAgent?.destroy();
    cachedUrl = proxy.href;
    cachedAgent = new HttpsProxyAgent(proxy);
  }
  return cachedAgent;
}
