"""Bounded, on-demand PRTS references. Never a game-control permission source.

All production HTTP requests target one public API origin, without credentials
or arbitrary URLs. No crawling, model calls, or game input occurs here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
import time
import urllib.error
from urllib.parse import quote, urlencode, urlsplit
import urllib.request

from .knowledge import search_terms


API_URL='https://prts.wiki/api.php'
MAX_BYTES=1024*1024
TIMEOUT=8


class WikiError(RuntimeError):
    pass


class WikiCancelled(WikiError):
    pass


def _cancel(event):
    if event is not None and event.is_set():
        raise WikiCancelled('资料查询已取消。')


class _FixedOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target=urlsplit(newurl)
        if (target.scheme,target.hostname,target.port,target.path)!=('https','prts.wiki',None,'/api.php') or target.username or target.password:
            raise WikiError('资料接口跳转到非允许地址，已停止查询。')
        return super().redirect_request(request,fp,code,message,headers,newurl)


class _PublicTransport:
    def __call__(self, params):
        request=urllib.request.Request(API_URL+'?'+urlencode(params),headers={
            'User-Agent':'ShuCompanion/0.1 (on-demand PRTS reference lookup)',
            'Accept':'application/json', 'Accept-Encoding':'identity',
        })
        try:
            opener=urllib.request.build_opener(_FixedOriginRedirect())
            with opener.open(request,timeout=TIMEOUT) as response:
                size=response.headers.get('Content-Length')
                if size and size.isdigit() and int(size)>MAX_BYTES:
                    raise WikiError('资料页面超过单次读取上限，未读取完整页面。')
                payload=response.read(MAX_BYTES+1)
                if len(payload)>MAX_BYTES:
                    raise WikiError('资料页面超过单次读取上限。')
                return json.loads(payload.decode('utf-8-sig'))
        except WikiError:
            raise
        except (OSError,UnicodeError,ValueError,urllib.error.URLError):
            raise WikiError('PRTS 资料接口暂时无法读取，请稍后重试。') from None


class _ArticleText(HTMLParser):
    _void={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    _skip={'script','style','nav','noscript','iframe','svg','form','button'}
    _classes={'navbox','navigation','mw-editsection','toc','catlinks','printfooter','metadata','noprint'}
    _blocks={'p','div','h1','h2','h3','h4','h5','section','li','tr','table','dl','dd','dt','br'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.parts=[];self.count=0
    def _append(self,text):
        if self.count<200000:
            text=text[:200000-self.count];self.parts.append(text);self.count+=len(text)
    def handle_starttag(self,tag,attrs):
        attributes=dict(attrs)
        classes=set((attributes.get('class') or '').split())
        dropped=(tag in self._skip or bool(classes & self._classes)
                 or attributes.get('role')=='navigation' or any(item[1] for item in self.stack))
        if not dropped and tag in self._blocks:self._append('\n')
        if not dropped and tag in {'td','th'}:self._append(' | ')
        if tag not in self._void:self.stack.append((tag,dropped))
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self._void:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                dropped=self.stack[index][1];del self.stack[index:]
                if not dropped and tag in self._blocks:self._append('\n')
                break
    def handle_data(self,data):
        if not any(item[1] for item in self.stack):self._append(data)
    def text(self):
        text=''.join(self.parts)
        text=re.sub(r'[^\S\n]+',' ',text)
        return re.sub(r'\n{3,}','\n\n',text).strip()


def _plain_html(source):
    parser=_ArticleText()
    parser.feed(source);parser.close()
    return parser.text()


def _excerpt(source,query,limit=1800):
    lines=[line.strip() for line in source.splitlines() if len(line.strip())>=4]
    if not lines:return source.strip()[:limit]
    terms=search_terms(query)
    ranked=[]
    for index,line in enumerate(lines):
        lowered=line.casefold()
        score=sum(len(term) for term in terms if term in lowered)
        if query.casefold() in lowered:score+=20
        ranked.append((score,index))
    anchors=[index for _,index in sorted(ranked,key=lambda item:(-item[0],item[1]))[:4]]
    # Keep nearby conditions/list items with the matching line, rather than
    # presenting isolated headings or dropping a preceding qualification.
    chosen=sorted({near for index in anchors for near in range(max(0,index-1),min(len(lines),index+3))})
    parts=[];remaining=limit
    for index in chosen:
        if remaining<20:break
        line=lines[index]
        # Keep a match near the visible excerpt when a large HTML table becomes
        # one long line. The text is still labelled partial, never a full guide.
        if len(line)>remaining:
            positions=[line.casefold().find(term) for term in terms if term in line.casefold()]
            position=min(positions) if positions else 0
            start=max(0,position-min(100,remaining//4))
            line=line[start:start+remaining]
        parts.append(line[:remaining]);remaining-=len(parts[-1])+1
    return '\n'.join(parts)[:limit]


def _number(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>0 else None


def _source_time(value):
    if not isinstance(value,str):return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return value if parsed.tzinfo is not None else None
    except ValueError:return None


def _canonical(title,pageid,candidate=''):
    if isinstance(candidate,str):
        try:
            parsed=urlsplit(candidate)
            if (parsed.scheme,parsed.hostname,parsed.port)==('https','prts.wiki',None) and parsed.path.startswith('/w/') and not parsed.username and not parsed.password and not parsed.query:
                return candidate
        except ValueError:pass
    return 'https://prts.wiki/w/'+quote(title.replace(' ','_'),safe='') if title else 'https://prts.wiki/?curid='+str(pageid)


class WikiKnowledge:
    """Public reference lookup with one-day cache, at most two pages/query.

    ``transport`` is an optional callable(params: dict)->dict for offline tests.
    Only query/search, page metadata/extracts, and parse are constructed here.
    Returned web text is untrusted reference data, never instructions to obey.
    """
    def __init__(self,store,*,transport=None,clock=time.time):
        self.store=store;self.transport=transport or _PublicTransport();self.clock=clock

    def _request(self,params,cancel_event):
        _cancel(cancel_event)
        try:
            result=self.transport(dict(params,format='json',formatversion=2,maxlag=5))
            if not isinstance(result,dict) or len(json.dumps(result,ensure_ascii=False).encode())>MAX_BYTES:
                raise WikiError('资料接口返回无效或过大的内容。')
        except WikiError:raise
        except (OSError,ValueError,TypeError):
            raise WikiError('PRTS 资料请求未完成。') from None
        _cancel(cancel_event)
        if result.get('error'):
            raise WikiError('PRTS 暂时未能提供所需资料。')
        return result

    @staticmethod
    def _finish(records,cached):
        output=[]
        for record in records[:4]:
            record=dict(record)
            record.update(partial=True,untrusted_reference=True,retrieval_cached=cached,
                          source_timestamp_known=record['version'].startswith('PRTS:revid:') and 'timestamp:unknown' not in record['version'])
            output.append(record)
        # Include all provenance/flags in the final Unicode JSON character cap.
        while len(json.dumps(output,ensure_ascii=False))>4000 and output:
            if len(output[-1]['text'])>64:
                output[-1]['text']=output[-1]['text'][:-max(1,len(output[-1]['text'])//8)]
                output[-1]['truncated']=True
            else:output.pop()
        return output

    def _cached(self,query):
        records=self.store.retrieve(query,top_k=4,char_budget=3000,token_budget=2200,
                                    source_kinds={'reference'},include_stale=False)
        return [record for record in records if urlsplit(record['source_url']).hostname=='prts.wiki']

    def lookup(self,query,cancel_event=None):
        if not isinstance(query,str) or not query.strip() or len(query.strip())>80 or any(ord(ch)<32 for ch in query):
            raise ValueError('资料检索词需要是1到80字的普通文本')
        query=query.strip()
        _cancel(cancel_event)
        cached=self._cached(query)
        _cancel(cancel_event)
        if cached:return self._finish(cached,True)
        search=self._request({'action':'query','list':'search','srsearch':query,
                              'srlimit':2,'srprop':'timestamp','srnamespace':0},cancel_event)
        hits=[]
        for hit in search.get('query',{}).get('search',[]):
            if not isinstance(hit,dict):continue
            pid=_number(hit.get('pageid'));title=hit.get('title')
            if pid and isinstance(title,str) and title.strip() and pid not in {item['pageid'] for item in hits}:
                hits.append({'pageid':pid,'title':title[:180],'timestamp':_source_time(hit.get('timestamp'))})
            if len(hits)==2:break
        if not hits:return []
        data=self._request({'action':'query','pageids':'|'.join(str(hit['pageid']) for hit in hits),
                            'prop':'extracts|revisions|info','explaintext':1,'exchars':6000,'exlimit':2,
                            'rvprop':'ids|timestamp','inprop':'url'},cancel_event)
        pages=data.get('query',{}).get('pages',[])
        if isinstance(pages,dict):pages=list(pages.values())
        pages={page.get('pageid'):page for page in pages if isinstance(page,dict)}
        added=[]
        for hit in hits:
            _cancel(cancel_event)
            pid=hit['pageid'];page=pages.get(pid,{})
            revisions=page.get('revisions',[])
            revision=revisions[0] if isinstance(revisions,list) and revisions and isinstance(revisions[0],dict) else {}
            revid=_number(revision.get('revid'))
            timestamp=_source_time(revision.get('timestamp')) or hit['timestamp']
            source=page.get('extract')
            if not isinstance(source,str) or not source.strip():
                parsed=self._request({'action':'parse','pageid':pid,'prop':'text|revid',
                                      'disablelimitreport':1,'disableeditsection':1},cancel_event).get('parse',{})
                html=parsed.get('text','')
                if isinstance(html,dict):html=html.get('*','')
                source=_plain_html(html) if isinstance(html,str) else ''
                revid=_number(parsed.get('revid')) or revid
            snippet=_excerpt(source,query)
            if not snippet.strip():continue
            version='PRTS:revid:'+str(revid or 'unknown')
            if timestamp is None:version+=';timestamp:unknown'
            _cancel(cancel_event)
            added.append(self.store.add('PRTS｜'+hit['title'],
                '[网页资料片段；非完整攻略，未经本机游戏验证]\n'+snippet,
                source_kind='reference',source_url=_canonical(hit['title'],pid,page.get('fullurl','')),
                version=version,updated_at=timestamp or float(self.clock()),ttl_seconds=86400,
                evidence_status='source_reported',external_id='prts:page:'+str(pid)+':query:'+query))
        _cancel(cancel_event)
        return self._finish(self._cached(query) or added,False)
